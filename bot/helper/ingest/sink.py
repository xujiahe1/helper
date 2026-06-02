"""Raw input → L1 抽取的"水槽"。

设计:
- 一条 raw_input 至多一条 L1Result(raw 级元信息: error / model / created_at)
- 同时写 0..N 条 L1Item(每个知识原子一行,(raw_id, idx) 主键)
- process_raw(raw_id) 同步 + 幂等:已有成功 L1Result 就跳过;失败 / 不存在则重跑
  重跑时先 DELETE 旧 L1Item 再写新的,避免 idx 残留
- run_async_l1(raw_id) 给 webhook 用:fire-and-forget,失败只 log 不抛
- backfill_pending() 扫所有缺 / 失败的,重跑

为啥 sync entry + asyncio 包一层:Athenai 是阻塞 SDK 调用,1s 回调窗口塞不下,
必须挤到独立线程里跑。
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import delete, select

from helper.ingest.l1_structure import L1Output, structure
from helper.llm.router import current_routing
from helper.storage import session
from helper.storage.models import L1Item, L1Result, RawInput

log = logging.getLogger(__name__)


def process_raw(
    raw_id: int,
    *,
    force: bool = False,
    user_instruction: str = "",
) -> L1Result | None:
    """跑 L1 → 写 L1Result(raw 级)+ 0..N 条 L1Item(原子级)。

    幂等: 已有 error="" 的 L1Result 直接返回,不重跑。force=True 强制重跑。
    重跑时 DELETE 旧 L1Item 后重写。

    群聊 @bot 路径会拉同 chat_id 30 分钟内 ≤20 条上下文 raw 一并喂 L1,
    让 LLM 把"被 @bot 那条很短"导致的 scene/signals/rationale 缺失从上下文补齐。

    user_instruction: 用户随同文档/链接发来的取舍指令(如"只读 xxx 部分")。
    只对当次抽取生效,不入库 — 同一篇 raw 下次重抽如果用户没再说,就按全文抽。
    """
    from helper.storage import raw_store

    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            log.warning("process_raw: raw_id=%s not found", raw_id)
            return None
        existing = s.get(L1Result, raw_id)
        if existing is not None and not existing.error and not force:
            return existing
        text = raw.content_text
        primary_speaker = raw.author_domain or ""
        chat_id = raw.chat_id or ""
        is_at_bot = bool(raw.is_at_bot)

        # 群聊 @bot → 拉上下文窗口(私聊 chat_id 空,跳过;非 @bot 也跳过)
        context_payload: list[dict] | None = None
        if chat_id and is_at_bot:
            ctx_rows = raw_store.list_chat_history(
                s,
                chat_id,
                since_minutes=30,
                limit=20,
                exclude_raw_id=raw_id,
            )
            if ctx_rows:
                context_payload = [
                    {
                        "raw_id": r.id,
                        "speaker": r.author_domain or "user",
                        "text": (r.content_text or "").strip(),
                        "ts": r.created_at.strftime("%H:%M") if r.created_at else "",
                    }
                    for r in ctx_rows
                ]

    out = structure(
        text,
        context=context_payload,
        primary_raw_id=raw_id,
        primary_speaker=primary_speaker,
        user_instruction=user_instruction,
    )
    routing = current_routing()
    model = routing.tasks["l1_structure"].model

    with session() as s:
        # raw 级 metadata: upsert
        existing = s.get(L1Result, raw_id)
        if existing is None:
            s.add(L1Result(raw_id=raw_id, error=out.error, model=model))
        else:
            existing.error = out.error
            existing.model = model

        # item 级: 重跑前先清,避免 idx 残留
        s.execute(delete(L1Item).where(L1Item.raw_id == raw_id))
        if out.ok:
            for idx, it in enumerate(out.items):
                s.add(L1Item(
                    raw_id=raw_id,
                    idx=idx,
                    type=it.type,
                    payload_json=json.dumps(it.payload, ensure_ascii=False),
                ))

        # 标 raw.processed
        if out.ok:
            raw = s.get(RawInput, raw_id)
            if raw is not None:
                raw.processed = True
            # 顺手把 raw 入向量索引 + FTS 词面索引(失败 log + 跳过,不阻塞主流程)
            try:
                from helper.storage import vector as vec
                vec.index_raw(s, raw_id)
            except Exception:  # noqa: BLE001
                log.exception("index_raw failed raw_id=%s", raw_id)
            try:
                from helper.storage import fts
                fts.index_raw(s, raw_id)
            except Exception:  # noqa: BLE001
                log.exception("fts.index_raw failed raw_id=%s", raw_id)
            # 逐条索引 section / decision(细粒度召回主力 — raw kind 是兜底,
            # 长文档真正命中靠 section)。重跑前先清掉旧 atom 索引,避免 idx 漂移残留。
            try:
                from helper.storage import vector as vec
                vec.delete_l1_atoms_for_raw(s, raw_id)
            except Exception:  # noqa: BLE001
                log.exception("vector.delete_l1_atoms_for_raw failed raw_id=%s", raw_id)
            try:
                from helper.storage import fts
                fts.delete_l1_atoms_for_raw(s, raw_id)
            except Exception:  # noqa: BLE001
                log.exception("fts.delete_l1_atoms_for_raw failed raw_id=%s", raw_id)
            for idx, it in enumerate(out.items):
                if it.type not in ("section", "decision"):
                    continue
                try:
                    from helper.storage import vector as vec
                    vec.index_l1_atom(s, raw_id, idx)
                except Exception:  # noqa: BLE001
                    log.exception("vec.index_l1_atom failed raw_id=%s idx=%s", raw_id, idx)
                try:
                    from helper.storage import fts
                    fts.index_l1_atom(s, raw_id, idx)
                except Exception:  # noqa: BLE001
                    log.exception("fts.index_l1_atom failed raw_id=%s idx=%s", raw_id, idx)
        s.commit()

    # L1 成功 → 串接 4 个候选 consumer(各自独立 session,失败互不影响)
    if out.ok:
        _run_consumers(raw_id)

    with session() as s:
        return s.get(L1Result, raw_id)


def _run_consumers(raw_id: int) -> None:
    """L1Item → 5 类候选表(concept/fact/case/relation;decision 留给 specgen 聚类)
    + 末尾跑追问 Engine 扫边界缺口 + 跑 ACL 打标。每个 consumer 独立 try/except,失败互不影响。

    注意: ACL 放在最后跑, 这样新建的候选行(consume_*)已经写入,
    tag_raw 内部会反查 raw_refs_json 把 topic 标继承到这些候选行。
    """
    try:
        from helper.ontology import consume_concept_items, consume_relation_items
        consume_concept_items(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("consume_concept_items failed raw_id=%s", raw_id)
    try:
        consume_relation_items(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("consume_relation_items failed raw_id=%s", raw_id)
    try:
        from helper.facts import consume_fact_items
        consume_fact_items(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("consume_fact_items failed raw_id=%s", raw_id)
    try:
        from helper.cases import consume_case_items
        consume_case_items(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("consume_case_items failed raw_id=%s", raw_id)
    try:
        from helper.inquiry import generate_inquiries
        generate_inquiries(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("generate_inquiries failed raw_id=%s", raw_id)
    try:
        from helper.conflict import detect_for_raw
        detect_for_raw(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("detect_for_raw failed raw_id=%s", raw_id)
    try:
        from helper.acl import tag_raw
        tag_raw(raw_id)
    except Exception:  # noqa: BLE001
        log.exception("acl tag_raw failed raw_id=%s", raw_id)


def _process_with_prefilter(raw_id: int) -> None:
    """群聊 listen 路径专用:先预筛,有判断信号才跑完整 L1。

    无信号的消息也写一条空 L1Result(error="filtered:...")标记已处理过,
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

    - prefilter=False(默认): 直接跑完整 L1
    - prefilter=True: 群聊 listen 路径,先关键词预筛 + mini 模型兜底
    没有 running loop(同步上下文,如 CLI)就直接同步跑。
    """
    from helper.im.queue import spawn

    task = spawn(_run_in_thread(raw_id, prefilter=prefilter))
    if task is None:
        if prefilter:
            _process_with_prefilter(raw_id)
        else:
            process_raw(raw_id)


def backfill_pending(*, limit: int = 50, force_all: bool = False) -> list[int]:
    """扫缺 L1 / L1 失败的 raw_input,重跑。返回处理过的 raw_id 列表。

    force_all=True: 把所有非 filtered 的 raw 全部用当前 prompt 版本重抽
    (用于 prompt 版本翻车后批量迁移; 注意会跑 LLM 调用)。
    """
    with session() as s:
        if force_all:
            todo = s.execute(
                select(RawInput.id)
                .outerjoin(L1Result, L1Result.raw_id == RawInput.id)
                .where(
                    (L1Result.raw_id.is_(None))
                    | (~L1Result.error.like("filtered:%"))
                )
                .order_by(RawInput.id.desc())
                .limit(limit)
            ).scalars().all()
            todo = list(todo)
        else:
            missing = s.execute(
                select(RawInput.id)
                .outerjoin(L1Result, L1Result.raw_id == RawInput.id)
                .where(L1Result.raw_id.is_(None))
                .order_by(RawInput.id.desc())
                .limit(limit)
            ).scalars().all()
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
