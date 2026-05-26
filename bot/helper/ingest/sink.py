"""Raw input → L1 结果的"水槽"。

设计:
- 每条 raw_input 至多一个 L1Result(以 raw_id 为主键)
- process_raw(raw_id) 同步 + 幂等:已有结果(无 error)就跳过;有 error / 不存在则重跑
- run_async_l1(raw_id) 给 webhook 用:fire-and-forget,失败只 log 不抛
- backfill_pending() 给 CLI 用:扫所有缺 L1 / L1 失败的,重跑

为啥 sync entry + asyncio 包一层:Athenai 是阻塞 SDK 调用,1s 回调窗口塞不下,
必须挤到独立线程里跑。M1 用 asyncio.to_thread 即可,M2 真要扛量再换 worker。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from helper.ingest.l1_structure import L1Structure, structure
from helper.llm.router import current_routing
from helper.storage import session
from helper.storage.models import L1Result, RawInput

log = logging.getLogger(__name__)


def _to_row(raw_id: int, l1: L1Structure, model: str) -> L1Result:
    import json

    return L1Result(
        raw_id=raw_id,
        scene=l1.scene,
        signals_json=json.dumps(l1.signals, ensure_ascii=False),
        tradeoffs_json=json.dumps(l1.tradeoffs, ensure_ascii=False),
        choice=l1.choice,
        rationale=l1.rationale,
        error=l1.error,
        model=model,
    )


def process_raw(raw_id: int, *, force: bool = False) -> L1Result | None:
    """跑 L1 → 写入 l1_results。失败也写(error 字段非空)。

    返回写入的行;raw_input 不存在或已有成功结果则返 None / 已存在的行。
    幂等:已有 error="" 的结果直接返回,不重跑;force=True 强制重跑。
    """
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            log.warning("process_raw: raw_id=%s not found", raw_id)
            return None
        existing = s.get(L1Result, raw_id)
        if existing is not None and not existing.error and not force:
            return existing
        text = raw.content_text

    l1 = structure(text)
    routing = current_routing()
    model = routing.tasks["l1_structure"].model

    with session() as s:
        existing = s.get(L1Result, raw_id)
        new_row = _to_row(raw_id, l1, model)
        if existing is None:
            s.add(new_row)
        else:
            existing.scene = new_row.scene
            existing.signals_json = new_row.signals_json
            existing.tradeoffs_json = new_row.tradeoffs_json
            existing.choice = new_row.choice
            existing.rationale = new_row.rationale
            existing.error = new_row.error
            existing.model = new_row.model
        s.flush()
        # 标 raw.processed(L1 出活就算 processed,后续 L2 / L3 各管各的)
        if not l1.error:
            raw = s.get(RawInput, raw_id)
            if raw is not None:
                raw.processed = True
        s.commit()
        return s.get(L1Result, raw_id)


def _process_with_prefilter(raw_id: int) -> None:
    """群聊 listen 路径专用:先预筛,有判断信号才跑完整 L1。

    无信号的消息也写一条空 L1Result(error="filtered")标记已处理过,
    避免被 backfill_pending 反复扫到再尝试。
    """
    from helper.ingest.prefilter import should_run_l1

    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return
        text = raw.content_text

    run_l1, reason = should_run_l1(text or "")
    log.info("l1_prefilter raw#%d reason=%s run=%s", raw_id, reason, run_l1)
    if run_l1:
        process_raw(raw_id)
        return

    # 不跑 L1,落一条 filtered 标记位,标 raw.processed=True
    with session() as s:
        existing = s.get(L1Result, raw_id)
        if existing is None:
            s.add(L1Result(raw_id=raw_id, error=f"filtered:{reason}", model="l1_prefilter"))
        raw = s.get(RawInput, raw_id)
        if raw is not None:
            raw.processed = True


async def _run_in_thread(raw_id: int, *, prefilter: bool) -> None:
    """L1 跑前等 LLM slot — 群聊 listen 路径会跑 mini prefilter + 可能 Sonnet L1,都走 semaphore。"""
    from helper.im.queue import llm_slot

    try:
        async with llm_slot():
            if prefilter:
                await asyncio.to_thread(_process_with_prefilter, raw_id)
            else:
                await asyncio.to_thread(process_raw, raw_id)
    except Exception:  # noqa: BLE001
        log.exception("background L1 failed raw_id=%s", raw_id)


def schedule_l1(raw_id: int, *, prefilter: bool = False) -> None:
    """给 webhook 用:在当前 event loop 里 fire-and-forget。

    - prefilter=False(默认): 直接跑完整 L1,主路径 / CLI 用
    - prefilter=True: 群聊 listen 路径,先关键词预筛 + mini 模型兜底
    没有 running loop(同步上下文,如 CLI)就直接同步跑——CLI ingest 本来也阻塞等结果。
    """
    from helper.im.queue import spawn

    task = spawn(_run_in_thread(raw_id, prefilter=prefilter))
    if task is None:
        if prefilter:
            _process_with_prefilter(raw_id)
        else:
            process_raw(raw_id)


def backfill_pending(*, limit: int = 50) -> list[int]:
    """扫缺 L1 / L1 失败的 raw_input,重跑。返回处理过的 raw_id 列表。"""
    with session() as s:
        # 缺 L1Result
        missing = s.execute(
            select(RawInput.id)
            .outerjoin(L1Result, L1Result.raw_id == RawInput.id)
            .where(L1Result.raw_id.is_(None))
            .order_by(RawInput.id.desc())
            .limit(limit)
        ).scalars().all()
        # 有 L1Result 但 error 非空(filtered:* 是预筛主动跳过,不算错误)
        errored = s.execute(
            select(L1Result.raw_id)
            .where(L1Result.error != "")
            .where(~L1Result.error.like("filtered:%"))
            .limit(limit)
        ).scalars().all()
    todo = list(missing) + [r for r in errored if r not in missing]
    for rid in todo:
        process_raw(rid, force=True)
    return todo
