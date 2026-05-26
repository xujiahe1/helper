"""对话级动作 — 主路径调这里完成 schedule 相关交互。

四个入口:
- handle_create(text, owner_user_id) -> 复述话术(写 ScheduleConfirm)
- handle_confirm(reply_text, owner_user_id) -> 落库 ScheduledTask + 删 confirm,或重置
- handle_list(owner_user_id) -> 列出该用户当前所有 enabled 任务
- handle_cancel(text, owner_user_id) -> 软删指定 #ID

约定:
- bot 复述格式固定为「我打算创建定时任务: {summary}。\n回 'yes' 确认,或重新描述以修改」
- ScheduleConfirm 5 分钟过期。再次创建覆盖旧 confirm
- receiver_id 默认 = owner_user_id, receiver_id_type = "user_id" (MVP 限定)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from helper.scheduler.parser import parse_request
from helper.storage import session
from helper.storage.models import ScheduleConfirm, ScheduledTask

log = logging.getLogger(__name__)

_CONFIRM_TTL_MIN = 5

_YES_TOKENS = ("yes", "y", "ok", "好", "对", "确认", "可以", "嗯", "yes!", "确定", "👍")


def is_yes_reply(text: str) -> bool:
    s = text.strip().lower().rstrip("。.!?！？ ")
    return s in _YES_TOKENS


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_pending_confirm(user_id: str) -> ScheduleConfirm | None:
    """看用户是否有 5 分钟内的待确认。返回过期的视为无。"""
    if not user_id:
        return None
    cutoff = _utcnow_naive() - timedelta(minutes=_CONFIRM_TTL_MIN)
    with session() as s:
        row = s.get(ScheduleConfirm, user_id)
        if row is None:
            return None
        if row.created_at < cutoff:
            s.delete(row)
            return None
        # 拷贝出 detached 副本
        snap = ScheduleConfirm(
            user_id=row.user_id,
            cron_expr=row.cron_expr,
            task_type=row.task_type,
            params_json=row.params_json,
            receiver_id=row.receiver_id,
            receiver_id_type=row.receiver_id_type,
            summary=row.summary,
            created_at=row.created_at,
        )
        return snap


def handle_create(text: str, owner_user_id: str) -> str:
    """解析 + 写 ScheduleConfirm,返回给用户的复述话术。

    owner_user_id 必须是域账号(user_id);为空就只能告诉用户"反查身份失败,无法创建"。
    """
    if not owner_user_id:
        return "暂时无法识别你的域账号,请稍后再试或联系管理员。"

    parsed, err = parse_request(text)
    if parsed is None:
        return f"❌ 没看懂你要创建的任务: {err}\n请用类似「每周一早上 9 点问我'本周项目进展'」的描述重试。"

    with session() as s:
        # 覆盖旧 confirm
        existing = s.get(ScheduleConfirm, owner_user_id)
        if existing is not None:
            s.delete(existing)
            s.flush()
        s.add(
            ScheduleConfirm(
                user_id=owner_user_id,
                cron_expr=parsed.cron_expr,
                task_type=parsed.task_type,
                params_json=json.dumps(parsed.params, ensure_ascii=False),
                receiver_id=owner_user_id,
                receiver_id_type="user_id",
                summary=parsed.summary,
            )
        )

    return (
        f"我打算创建定时任务: {parsed.summary}\n"
        f"(cron: `{parsed.cron_expr}`)\n"
        f"回 'yes' 确认,或重新描述以修改。{_CONFIRM_TTL_MIN} 分钟内有效。"
    )


def handle_confirm(reply_text: str, owner_user_id: str) -> str | None:
    """处理用户对 pending confirm 的回应。

    返回:
      - None: 当前用户没有 pending confirm(让上层走其他 intent)
      - 文案 str: 给用户的反馈
    """
    pending = get_pending_confirm(owner_user_id)
    if pending is None:
        return None
    if not is_yes_reply(reply_text):
        # 不是 yes — 走 handle_create 重新解析,可能用户在改描述
        return handle_create(reply_text, owner_user_id)

    # yes — 落 ScheduledTask
    with session() as s:
        task = ScheduledTask(
            owner_user_id=pending.user_id,
            cron_expr=pending.cron_expr,
            task_type=pending.task_type,
            params_json=pending.params_json,
            receiver_id=pending.receiver_id,
            receiver_id_type=pending.receiver_id_type,
            summary=pending.summary,
            enabled=True,
        )
        s.add(task)
        s.flush()
        new_id = task.id
        # 删除 confirm
        confirm_row = s.get(ScheduleConfirm, owner_user_id)
        if confirm_row is not None:
            s.delete(confirm_row)
    return f"✅ 已创建定时任务 #{new_id}: {pending.summary}"


def handle_list(owner_user_id: str) -> str:
    if not owner_user_id:
        return "暂时无法识别你的域账号。"
    with session() as s:
        from sqlalchemy import select

        rows = s.execute(
            select(ScheduledTask)
            .where(ScheduledTask.owner_user_id == owner_user_id, ScheduledTask.enabled.is_(True))
            .order_by(ScheduledTask.id.desc())
        ).scalars().all()
        if not rows:
            return "你还没有定时任务。说一句「每周一 9 点问我'本周项目进展'」就能创建。"
        lines = [f"你的定时任务({len(rows)} 条):"]
        for r in rows:
            lines.append(f"  #{r.id}  {r.summary}  (cron: {r.cron_expr})")
        lines.append("\n取消用「取消 #N」")
        return "\n".join(lines)


def handle_cancel(text: str, owner_user_id: str) -> str:
    """从 text 抠出 #N 或纯数字 N,软删该 task(仅本人创建的)。"""
    import re

    m = re.search(r"#?(\d+)", text)
    if not m:
        return "请告诉我要取消的任务编号,例:取消 #3"
    task_id = int(m.group(1))
    with session() as s:
        row = s.get(ScheduledTask, task_id)
        if row is None:
            return f"找不到任务 #{task_id}"
        if row.owner_user_id != owner_user_id:
            return f"任务 #{task_id} 不是你创建的,无法取消"
        if not row.enabled:
            return f"任务 #{task_id} 已经是取消状态"
        row.enabled = False
        return f"✅ 已取消任务 #{task_id}: {row.summary}"
