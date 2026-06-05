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
    """LLM 返一条 valid 策略 + 一条无效 → 只写 valid。

    新判据下 valid 只有 gap_trigger / gap_action 两个。
    """
    from sqlalchemy import select

    from helper.inquiry import generate_inquiries
    from helper.storage import session
    from helper.storage.models import InquiryLog

    rid = make_raw("周一发版")
    _seed_decision(rid)

    valid_id = "gap_trigger"

    llm_stub.set("elicit", json.dumps([
        {"strategy_id": valid_id, "question": "这条 decision 适用什么类型的发版?",
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
    sid = "gap_action"
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


def test_run_inquiry_audit_drops_studious_keeps_real_gap(db, settings, llm_stub, make_raw):
    """复审存量未答 inquiry: LLM judge 学究式 → drop (sentinel close), 真 G1/G2 → keep。

    drop 后 hit='no' + answer_raw_id=0 (sentinel, 跟真答复绑定区分),
    `answer_raw_id IS NULL` 过滤把 sentinel 一并排除, 不再进周报。
    """
    import json
    from helper.inquiry import run_inquiry_audit
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("q")
    with session() as s:
        i_drop = InquiryLog(raw_id=qrid, strategy_id="legacy", question="有没有踩过坑?反例?")
        i_keep = InquiryLog(raw_id=qrid, strategy_id="legacy", question="走 lml 流程具体走哪一步?")
        s.add(i_drop)
        s.add(i_keep)
        s.flush()
        drop_id, keep_id = i_drop.id, i_keep.id

    # 用 callable 按 user 内容判 — 看到"踩过坑"判 drop, 看到"lml 流程"判 keep
    def _audit_handler(system, user, **kw):
        if "踩过坑" in user:
            return json.dumps({"verdict": "drop", "reason": "命中反例类禁问"}, ensure_ascii=False)
        return json.dumps({"verdict": "keep", "reason": "G2 动作不可复现"}, ensure_ascii=False)

    llm_stub.set("memory_audit", _audit_handler)

    report = run_inquiry_audit()
    assert report.audited == 2
    assert report.kept == 1
    assert len(report.dropped) == 1
    assert report.dropped[0].inquiry_id == drop_id

    with session() as s:
        d = s.get(InquiryLog, drop_id)
        k = s.get(InquiryLog, keep_id)
        assert d.hit == "no"
        assert d.answer_raw_id == 0  # sentinel
        assert k.hit == "unknown"
        assert k.answer_raw_id is None  # 未动


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
