"""conflict.detect_for_raw — 各 verdict 分支 + 幂等。"""

from __future__ import annotations

import json


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


def test_resolve_marks_log(db, settings, make_raw):
    from datetime import datetime

    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog

    rid = make_raw("x")
    with session() as s:
        c = ConflictLog(raw_id=rid, spec_slug="spec-a", summary="s", severity="medium")
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
