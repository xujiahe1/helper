"""conflict.detect_for_raw — 各 verdict 分支 + 幂等。"""

from __future__ import annotations

import json

from sqlalchemy import select  # noqa: F401  # 老测试在函数体里又 import,这里给新测试用


def _seed_decision(raw_id: int, scene: str = "S", choice: str = "C") -> None:
    from helper.storage import session
    from helper.storage.models import L1Item

    with session() as s:
        s.add(L1Item(
            raw_id=raw_id,
            idx=0,
            type="decision",
            payload_json=json.dumps({
                "scene": scene,
                "signals": ["a"],
                "tradeoffs": [],
                "choice": choice,
                "rationale": "R",
            }, ensure_ascii=False),
        ))


def test_contradicts_writes_conflict_log(db, settings, llm_stub, retrieve_stub, make_raw):
    from sqlalchemy import select

    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("某条决策原文")
    _seed_decision(rid, scene="发版前", choice="周一发")
    retrieve_stub.set([retrieve_stub.hit(
        "spec", "release-no-monday", title="周一不发版", body="周一禁发版本"
    )])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "contradicts",
        "summary": "周一发 vs 周一禁发",
        "severity": "high",
    }, ensure_ascii=False))

    hits = detect_for_raw(rid)
    assert len(hits) == 1
    assert hits[0].spec_slug == "release-no-monday"
    assert hits[0].severity == "high"

    with session() as s:
        rows = list(s.execute(select(ConflictLog).where(ConflictLog.raw_id == rid)).scalars())
    assert len(rows) == 1
    assert rows[0].resolution == "open"


def test_refines_does_not_write(db, settings, llm_stub, retrieve_stub, make_raw):
    from sqlalchemy import select

    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("x")
    _seed_decision(rid)
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", '{"verdict":"refines","summary":"","severity":"low"}')

    hits = detect_for_raw(rid)
    assert hits == []

    with session() as s:
        rows = list(s.execute(select(ConflictLog).where(ConflictLog.raw_id == rid)).scalars())
    assert rows == []


def test_no_decision_returns_empty(db, settings, llm_stub, retrieve_stub, make_raw):
    """raw 没出 decision 原子 → 直接返空,不调 LLM 不调 retrieve。"""
    from helper.conflict import detect_for_raw

    rid = make_raw("没有决策")
    # 不种 L1Item

    hits = detect_for_raw(rid)
    assert hits == []
    judge_calls = [c for c in llm_stub.calls if c[0] == "conflict_judge"]
    assert judge_calls == []


def test_idempotent_on_existing_open_conflict(db, settings, llm_stub, retrieve_stub, make_raw):
    """同一 (raw_id, spec_slug, open) 已存在 → 不重复 INSERT,复用旧行 id。"""
    from sqlalchemy import select

    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("x")
    _seed_decision(rid)
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", '{"verdict":"contradicts","summary":"s1","severity":"medium"}')

    hits1 = detect_for_raw(rid)
    assert len(hits1) == 1
    first_log_id = hits1[0].log_id

    hits2 = detect_for_raw(rid)
    assert len(hits2) == 1
    assert hits2[0].log_id == first_log_id

    with session() as s:
        rows = list(s.execute(
            select(ConflictLog).where(ConflictLog.raw_id == rid)
        ).scalars())
    assert len(rows) == 1


def test_verdict_none_does_not_write(db, settings, llm_stub, retrieve_stub, make_raw):
    """verdict=none(paraphrase / 无关)→ 直接丢,不入 ConflictLog。新 detector 的核心收口。"""
    from sqlalchemy import select

    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("x")
    _seed_decision(rid)
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "none", "summary": "", "severity": "low",
    }, ensure_ascii=False))

    hits = detect_for_raw(rid)
    assert hits == []
    with session() as s:
        rows = list(s.execute(select(ConflictLog).where(ConflictLog.raw_id == rid)).scalars())
    assert rows == []


def test_severity_high_keeps_open(db, settings, llm_stub, retrieve_stub, make_raw):
    """severity=high 且 LLM 没给 auto_resolution → 留 open 等人裁。"""
    from helper.conflict import detect_for_raw

    rid = make_raw("x")
    _seed_decision(rid)
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "contradicts", "summary": "颠覆性冲突",
        "severity": "high", "auto_resolution": "", "auto_reason": "",
    }, ensure_ascii=False))

    hits = detect_for_raw(rid)
    assert len(hits) == 1
    assert hits[0].resolution == "open"
    assert hits[0].severity == "high"


def test_severity_medium_auto_supersedes(db, settings, llm_stub, retrieve_stub, make_raw):
    """severity=medium 且无 auto_resolution → 代码兜底 newest-wins,auto_superseded 落库。"""
    from datetime import datetime

    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate

    rid = make_raw("x")
    _seed_decision(rid)
    # 种一个 SpecCandidate,断言被打上 superseded_at
    with session() as s:
        s.add(SpecCandidate(slug="spec-a", title="t", statement="..."))
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "contradicts", "summary": "中等冲突",
        "severity": "medium",
    }, ensure_ascii=False))

    hits = detect_for_raw(rid)
    assert len(hits) == 1
    assert hits[0].resolution == "auto_superseded"

    with session() as s:
        cl = s.execute(select(ConflictLog).where(ConflictLog.raw_id == rid)).scalar_one()
        assert cl.resolution == "auto_superseded"
        assert cl.resolved_by == "auto-judge"
        assert isinstance(cl.resolved_at, datetime)
        assert cl.auto_reason

        sc = s.execute(select(SpecCandidate).where(SpecCandidate.slug == "spec-a")).scalar_one()
        assert sc.superseded_at is not None
        assert sc.superseded_by == rid


def test_llm_auto_resolution_respected(db, settings, llm_stub, retrieve_stub, make_raw):
    """LLM 给 auto_resolution=rejected(权威规则要求保留旧)→ 走 auto_rejected,不 supersede 旧。"""
    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate

    rid = make_raw("x")
    _seed_decision(rid)
    with session() as s:
        s.add(SpecCandidate(slug="spec-a", title="t", statement="..."))
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "contradicts", "summary": "...",
        "severity": "medium",
        "auto_resolution": "rejected",
        "auto_reason": "memory 里 IAM 领域以刘佳翔为准,新输入非他",
    }, ensure_ascii=False))

    hits = detect_for_raw(rid)
    assert len(hits) == 1
    assert hits[0].resolution == "auto_rejected"

    with session() as s:
        cl = s.execute(select(ConflictLog).where(ConflictLog.raw_id == rid)).scalar_one()
        assert cl.resolution == "auto_rejected"
        # auto_rejected 不 supersede 旧候选
        sc = s.execute(select(SpecCandidate).where(SpecCandidate.slug == "spec-a")).scalar_one()
        assert sc.superseded_at is None


def test_memory_directives_passed_to_judge(db, settings, llm_stub, retrieve_stub, make_raw):
    """命中 entity 的 alive memory 拼进 judge 的 user prompt,LLM 能看到权威规则。"""
    from helper.conflict import detect_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="发版前",
            directive="发版相关以王一鸣说的为准",
        ))

    rid = make_raw("x")
    _seed_decision(rid, scene="发版前", choice="周一发")
    retrieve_stub.set([retrieve_stub.hit("spec", "spec-a", body="...")])
    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "none", "summary": "", "severity": "low",
    }, ensure_ascii=False))

    detect_for_raw(rid)
    judge_calls = [c for c in llm_stub.calls if c[0] == "conflict_judge"]
    assert len(judge_calls) == 1
    user_prompt = judge_calls[0][2]
    assert "权威规则" in user_prompt
    assert "发版相关以王一鸣说的为准" in user_prompt


# ---------- rejudge ----------

def test_rejudge_closes_verdict_none(db, settings, llm_stub, make_raw):
    """rejudge: 已 open 的 ConflictLog 重判 verdict=none → auto_rejected close。"""
    from helper.conflict.rejudge import rejudge_open_conflicts
    from helper.storage import session
    from helper.storage.models import ConflictLog, L1Item, SpecCandidate

    with session() as s:
        s.add(SpecCandidate(
            slug="spec-old", title="老规约", statement="老结论",
        ))
    rid = make_raw("新输入")
    with session() as s:
        s.add(L1Item(
            raw_id=rid, idx=0, type="decision",
            payload_json=json.dumps({
                "scene": "X 场景", "choice": "新选择", "rationale": "...",
            }, ensure_ascii=False),
        ))
        s.add(ConflictLog(
            raw_id=rid, target_type="spec", target_slug="spec-old",
            summary="老 vs 新(paraphrase)", severity="medium",
            resolution="open",
        ))

    llm_stub.set("conflict_judge", json.dumps({
        "verdict": "none", "summary": "", "severity": "low",
    }, ensure_ascii=False))

    stats = rejudge_open_conflicts()
    assert stats["rejected_none"] == 1

    with session() as s:
        cl = s.execute(select(ConflictLog)).scalar_one()
        assert cl.resolution == "auto_rejected"
        assert cl.resolved_by == "auto-rejudge"
        assert "verdict=none" in cl.auto_reason


def test_resolve_marks_log(db, settings, make_raw):
    from datetime import datetime

    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("x")
    with session() as s:
        c = ConflictLog(raw_id=rid, target_slug="spec-a", summary="s", severity="medium")
        s.add(c)
        s.flush()
        log_id = c.id

    ok = resolve(log_id, resolution="superseded", resolver_domain="owner")
    assert ok is True
    with session() as s:
        row = s.get(ConflictLog, log_id)
        assert row.resolution == "superseded"
        assert row.resolved_by == "owner"
        assert isinstance(row.resolved_at, datetime)
