"""ingest.process_raw — L1 抽取 + L1Item 写入 + raw.processed 翻位 + 串接 consumers。"""

from __future__ import annotations

import json

from sqlalchemy import select


def test_process_raw_writes_l1items(db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw):
    """LLM 返 1 条 decision + 1 条 fact → 2 个 L1Item + L1Result.error 空 + raw.processed=True。"""
    from helper.ingest.sink import process_raw
    from helper.storage import session
    from helper.storage.models import L1Item, L1Result, RawInput

    llm_stub.set("l1_structure", json.dumps([
        {"type": "decision", "scene": "S", "signals": ["a"], "tradeoffs": [],
         "choice": "C", "rationale": "R"},
        {"type": "fact", "subject": "X", "predicate": "is", "object": "Y", "scope": ""},
    ], ensure_ascii=False))
    # consumers: 追问 + 冲突 — 让它们返空,测主链路写入即可
    llm_stub.set("elicit", "[]")
    # conflict 不会被调用,因为 retrieve_stub 默认返空 → spec_hits 空 → 不进 LLM

    rid = make_raw("某条决策原文")
    out = process_raw(rid)
    assert out is not None
    assert out.error == ""

    with session() as s:
        items = list(s.execute(select(L1Item).where(L1Item.raw_id == rid)).scalars())
        assert len(items) == 2
        types = {it.type for it in items}
        assert types == {"decision", "fact"}
        raw = s.get(RawInput, rid)
        assert raw.processed is True


def test_process_raw_idempotent_skip_on_success(
    db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw
):
    """已成功 L1Result 二次调用直接复用,不再调 LLM。"""
    from helper.ingest.sink import process_raw

    llm_stub.set("l1_structure", "[]")
    llm_stub.set("elicit", "[]")
    rid = make_raw("noop")
    process_raw(rid)
    n_before = len(llm_stub.calls)
    process_raw(rid)
    # 二次调用不应再触发 l1_structure
    extra = [c for c in llm_stub.calls[n_before:] if c[0] == "l1_structure"]
    assert extra == []


def test_process_raw_force_rerun_replaces_items(
    db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw
):
    """force=True 重跑会清掉旧 L1Item 再写新的。"""
    from sqlalchemy import select

    from helper.ingest.sink import process_raw
    from helper.storage import session
    from helper.storage.models import L1Item

    llm_stub.set("l1_structure", json.dumps(
        [{"type": "fact", "subject": "A", "predicate": "is", "object": "B"}],
        ensure_ascii=False,
    ))
    llm_stub.set("elicit", "[]")
    rid = make_raw("x")
    process_raw(rid)

    # 改返回结果再 force 跑一次
    llm_stub.set("l1_structure", json.dumps(
        [
            {"type": "concept", "name": "Foo", "entity_type": "term", "description": "d"},
            {"type": "concept", "name": "Bar", "entity_type": "term", "description": "d"},
        ],
        ensure_ascii=False,
    ))
    process_raw(rid, force=True)

    with session() as s:
        items = list(s.execute(select(L1Item).where(L1Item.raw_id == rid)).scalars())
    assert {it.type for it in items} == {"concept"}
    assert len(items) == 2


def test_process_raw_llm_failure_records_error(
    db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw
):
    """LLM 抛错被捕获,写 L1Result.error,不抛、不写 L1Item、不翻 processed。"""
    from sqlalchemy import select

    from helper.ingest.sink import process_raw
    from helper.storage import session
    from helper.storage.models import L1Item, L1Result, RawInput

    def _boom(**kw):
        raise RuntimeError("athenai 503")

    llm_stub.set("l1_structure", _boom)

    rid = make_raw("x")
    out = process_raw(rid)
    assert out is not None
    assert "LLM call failed" in out.error

    with session() as s:
        items = list(s.execute(select(L1Item).where(L1Item.raw_id == rid)).scalars())
        raw = s.get(RawInput, rid)
    assert items == []
    assert raw.processed is False


# ---------- backfill_pending: 显式跳过的 raw 不再被当成漏抽 ----------


def test_backfill_pending_skips_filtered_skipped_purged(
    db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw
):
    """backfill_pending / force_all 都不能扫到 error in (filtered:*, skipped:*, purged:*)
    的 raw, 否则 ask 路径的问句会被反复"漏抽"重抽 L1 (历史污染原因)。
    """
    from helper.ingest.sink import backfill_pending
    from helper.storage import session
    from helper.storage.models import L1Result

    rid_filtered = make_raw("好的收到")
    rid_skipped = make_raw("刘佳翔就是哥吗?")
    rid_purged = make_raw("某条以前抽过的问句")
    rid_real_pending = make_raw("决定下线某个功能")

    with session() as s:
        s.add(L1Result(raw_id=rid_filtered, error="filtered:llm_no", model="l1_prefilter"))
        s.add(L1Result(raw_id=rid_skipped, error="skipped:ask_path", model="ask_route"))
        s.add(L1Result(raw_id=rid_purged, error="purged:question", model="purge"))
        s.commit()

    # 让 process_raw 的 LLM 调用返回空 items, 避免 conftest 没设 stub 报错
    llm_stub.set("l1_structure", "[]")

    todo = backfill_pending(limit=50)
    # 只该跑 rid_real_pending (它没 L1Result, 是真正的 pending)
    assert rid_filtered not in todo
    assert rid_skipped not in todo
    assert rid_purged not in todo
    assert rid_real_pending in todo


def test_backfill_force_all_also_skips_filtered_skipped_purged(
    db, settings, llm_stub, retrieve_stub, stub_index_raw, make_raw
):
    from helper.ingest.sink import backfill_pending
    from helper.storage import session
    from helper.storage.models import L1Result

    rid_skipped = make_raw("谁是哥?")
    rid_errored = make_raw("LLM 真的失败过的")  # 真正应被 force_all 重抽

    with session() as s:
        s.add(L1Result(raw_id=rid_skipped, error="skipped:ask_path", model="ask_route"))
        s.add(L1Result(raw_id=rid_errored, error="LLM call failed: 503", model="l1"))
        s.commit()

    llm_stub.set("l1_structure", "[]")

    todo = backfill_pending(limit=50, force_all=True)
    assert rid_skipped not in todo
    assert rid_errored in todo
