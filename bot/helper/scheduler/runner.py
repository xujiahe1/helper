"""APScheduler AsyncIOScheduler — bot 进程内每分钟扫一次 scheduled_tasks 表。

设计:
- 不为每个 task 在 scheduler 里建独立 job,只挂一个 1min IntervalTrigger 全表扫
- DB 是唯一来源:重启 / 改 cron / 软删,不需要同步 scheduler 状态
- 每行用 last_run_at 做幂等:同一分钟扫到不重跑
- 任务执行 fire-and-forget,失败 log 不影响其他任务
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from helper.scheduler.parser import build_trigger
from helper.storage import session
from helper.storage.models import ScheduledTask

# 用户说「早上 9 点」默认指 CST,cron 按 CST 评估;DB 里仍存 naive UTC datetime
# (与表中其它 created_at 一致)。如果服务器系统时区不是 CST,这里也对齐。
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _utcnow_naive() -> datetime:
    """SQLite 存的是 naive UTC datetime,这里和它们对齐。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _should_fire(cron_expr: str, last_run_at: datetime | None, now: datetime) -> bool:
    """这一分钟是否到点。

    用 CronTrigger.get_next_fire_time(prev, now) 拿"上次基准之后的下一个触发点",
    如果它 <= now,说明这一分钟内有触发点。
    last_run_at 为空时用 now-2 分钟做 prev。
    """
    try:
        trig = build_trigger(cron_expr, _LOCAL_TZ)
    except Exception as e:  # noqa: BLE001
        log.warning("invalid cron in DB: %s err=%s", cron_expr, e)
        return False
    prev = last_run_at if last_run_at is not None else now - timedelta(minutes=2)
    # 内部存 naive UTC,这里补回 utc 后让 trigger 内部转 CST
    prev_aware = prev.replace(tzinfo=timezone.utc) if prev.tzinfo is None else prev
    now_aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    next_fire = trig.get_next_fire_time(prev_aware, prev_aware)
    if next_fire is None:
        return False
    return next_fire <= now_aware


async def _execute_task(task_id: int) -> None:
    """单个任务执行。失败只 log。"""
    from helper.scheduler.tasks import dispatch

    with session() as s:
        task = s.get(ScheduledTask, task_id)
        if task is None or not task.enabled:
            return
        # 复制需要的字段,避免离开 session 后访问
        snapshot = {
            "id": task.id,
            "task_type": task.task_type,
            "params_json": task.params_json,
            "receiver_id": task.receiver_id,
            "receiver_id_type": task.receiver_id_type,
            "owner_user_id": task.owner_user_id,
            "summary": task.summary,
        }
        # 先标 last_run_at,避免本分钟重复进入(下面如果失败也不重试本次)
        task.last_run_at = _utcnow_naive()

    try:
        await dispatch(snapshot)
    except Exception:  # noqa: BLE001
        log.exception("scheduled task #%d execution failed", task_id)


async def _tick() -> None:
    """每分钟扫表 + 触发到点的任务。"""
    now = _utcnow_naive()
    try:
        with session() as s:
            tasks = s.execute(
                select(ScheduledTask).where(ScheduledTask.enabled.is_(True))
            ).scalars().all()
            due_ids = []
            for t in tasks:
                # 同一分钟已跑过 -> skip
                if t.last_run_at and (now - t.last_run_at).total_seconds() < 50:
                    continue
                if _should_fire(t.cron_expr, t.last_run_at, now):
                    due_ids.append(t.id)
    except Exception:  # noqa: BLE001
        log.exception("scheduler tick: scan failed")
        return

    for tid in due_ids:
        # 不要 await 一个一个串行;用 create_task 让它们并发跑
        # 但 _execute_task 自己有 session,不能跨 await 共享
        import asyncio

        asyncio.create_task(_execute_task(tid))


def ensure_owner_inbox_weekly() -> None:
    """配了 helper_owner_domain 但没有 inbox_weekly 任务 → 自动建一条默认 0 9 * * 1。

    幂等: 已有同 owner+task_type='inbox_weekly' 行(无论 enabled)就不动,避免覆盖
    用户手动改的 cron / 触碰已被软删的旧任务。
    """
    from helper.config import get_settings

    owner = get_settings().helper_owner_domain
    if not owner:
        return
    with session() as s:
        existing = s.execute(
            select(ScheduledTask)
            .where(ScheduledTask.owner_user_id == owner)
            .where(ScheduledTask.task_type == "inbox_weekly")
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return
        s.add(ScheduledTask(
            owner_user_id=owner,
            cron_expr="0 9 * * 1",
            task_type="inbox_weekly",
            params_json="{}",
            receiver_id=owner,
            receiver_id_type="user_id",
            summary=f"Inbox 周报 — 每周一 09:00 推送给 {owner}",
            enabled=True,
        ))
    log.info("auto-created inbox_weekly task for owner=%s (cron 0 9 * * 1)", owner)


def ensure_spec_topic_scan_daily() -> None:
    """改动 3: 没 spec_topic_scan 任务 → 自动建一条每天 03:00 跑。

    不挂在 owner 上 (跨 owner 的 topic 都扫), 用 receiver_id="" 占位避免 dispatch
    误发消息 — _spec_topic_scan 不读 receiver_id, 只跑后台 draft。
    幂等: 已存在 (无论 enabled) 就不动。
    """
    with session() as s:
        existing = s.execute(
            select(ScheduledTask)
            .where(ScheduledTask.task_type == "spec_topic_scan")
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return
        s.add(ScheduledTask(
            owner_user_id="system",
            cron_expr="0 3 * * *",
            task_type="spec_topic_scan",
            params_json="{}",
            receiver_id="",
            receiver_id_type="user_id",
            summary="每日 03:00 扫 SpecTopic, 满足饱和/静默判据的簇触发 spec draft",
            enabled=True,
        ))
    log.info("auto-created spec_topic_scan task (daily 03:00)")


def start_scheduler() -> None:
    """FastAPI startup 调。重复调用安全(已启动则忽略)。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    try:
        ensure_owner_inbox_weekly()
    except Exception:  # noqa: BLE001
        log.exception("ensure_owner_inbox_weekly failed")
    try:
        ensure_spec_topic_scan_daily()
    except Exception:  # noqa: BLE001
        log.exception("ensure_spec_topic_scan_daily failed")
    _scheduler = AsyncIOScheduler(timezone=timezone.utc)
    # 每分钟 tick 一次。第一次延迟 5 秒避免与 startup 抢 event loop
    from apscheduler.triggers.interval import IntervalTrigger

    _scheduler.add_job(
        _tick,
        IntervalTrigger(minutes=1, start_date=_utcnow_naive() + timedelta(seconds=5)),
        id="schedule_tick",
        replace_existing=True,
    )
    _scheduler.add_job(
        _bot_routing_expire_tick,
        IntervalTrigger(minutes=1, start_date=_utcnow_naive() + timedelta(seconds=10)),
        id="bot_routing_expire",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("scheduler started — 每分钟扫 scheduled_tasks + 路由超时清理")


def _bot_routing_expire_tick() -> None:
    """每分钟扫一次 PendingRouting, 5min 没回的标 expired 并通知用户。"""
    try:
        from helper.im.bot_routing import expire_old_routings
        n = expire_old_routings()
        if n:
            log.info("bot routing expired %d entries", n)
    except Exception:  # noqa: BLE001
        log.exception("bot routing expire tick failed")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
    _scheduler = None
