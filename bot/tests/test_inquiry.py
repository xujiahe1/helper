"""inquiry.generate_inquiries — LLM 返追问 → 落 inquiry_log。"""

from __future__ import annotations

import json


def _seed_decision(raw_id: int) -> None:
    from helper.storage import session
    from helper.storage.models import L1Item

    with session() as s:
        s.add(L1Item(
            raw_id=raw_id,
            idx=0,
            type="decision",
            payload_json=json.dumps({
                "scene": "发版", "signals": [], "tradeoffs": [],
                "choice": "周一发", "rationale": "习惯",
            }, ensure_ascii=False),
        ))


def test_generate_inquiries_writes_log(db, settings, llm_stub, make_raw):
    """LLM 返一条 valid 策略 + 一条无效 → 只写 valid。"""
    from sqlalchemy import select

    from helper.inquiry import generate_inquiries
    from helper.storage import session
    from helper.storage.models import InquiryLog

    rid = make_raw("周一发版")
    _seed_decision(rid)

    # 用默认 inquiry_strategies.yaml 里有的 id。看下默认有哪些。
    # 先随便用一个 strategy_id;若过滤掉就空,我们再调整。
    from helper.inquiry.engine import load_strategies
    strategies = load_strategies()
    assert strategies, "default strategies yaml 应有内容"
    valid_id = strategies[0]["id"]

    llm_stub.set("elicit", json.dumps([
        {"strategy_id": valid_id, "question": "如果遇到紧急修复呢?",
         "target_l1_idx": 0, "priority": 80},
        {"strategy_id": "__unknown_strategy__", "question": "无效",
         "target_l1_idx": 0, "priority": 90},
    ], ensure_ascii=False))

    hits = generate_inquiries(rid)
    assert len(hits) == 1
    assert hits[0].strategy_id == valid_id

    with session() as s:
        rows = list(s.execute(
            select(InquiryLog).where(InquiryLog.raw_id == rid)
        ).scalars())
    assert len(rows) == 1
    assert rows[0].strategy_id == valid_id


def test_generate_inquiries_no_decision_skips(db, settings, llm_stub, make_raw):
    """没 decision 原子 → 直接返空,不调 LLM。"""
    from helper.inquiry import generate_inquiries

    rid = make_raw("noop")
    hits = generate_inquiries(rid)
    assert hits == []
    assert all(c[0] != "elicit" for c in llm_stub.calls)


def test_generate_inquiries_idempotent_clears_unanswered(db, settings, llm_stub, make_raw):
    """重跑会清旧的未答 inquiry,以新结果覆盖。"""
    from sqlalchemy import select

    from helper.inquiry import generate_inquiries
    from helper.inquiry.engine import load_strategies
    from helper.storage import session
    from helper.storage.models import InquiryLog

    rid = make_raw("x")
    _seed_decision(rid)
    sid = load_strategies()[0]["id"]
    llm_stub.set("elicit", json.dumps(
        [{"strategy_id": sid, "question": "q1", "target_l1_idx": 0, "priority": 50}],
        ensure_ascii=False,
    ))
    generate_inquiries(rid)

    llm_stub.set("elicit", json.dumps(
        [{"strategy_id": sid, "question": "q2", "target_l1_idx": 0, "priority": 50}],
        ensure_ascii=False,
    ))
    generate_inquiries(rid)

    with session() as s:
        rows = list(s.execute(
            select(InquiryLog).where(InquiryLog.raw_id == rid)
        ).scalars())
    assert len(rows) == 1
    assert rows[0].question == "q2"


def test_record_answer_binds_inquiry(db, settings, make_raw):
    from helper.inquiry import generate_inquiries  # noqa: F401 ensure module import
    from helper.inquiry.engine import record_answer
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("q")
    arid = make_raw("a")
    with session() as s:
        iq = InquiryLog(raw_id=qrid, strategy_id="bound_check", question="?")
        s.add(iq)
        s.flush()
        iq_id = iq.id

    record_answer(iq_id, arid)
    with session() as s:
        iq = s.get(InquiryLog, iq_id)
        assert iq.answer_raw_id == arid
