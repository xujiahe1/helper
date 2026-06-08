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


# ─── 改动 5: 已批准 spec 防覆写 ──────────────────────────────────


def _make_approved_spec(slug: str, statement: str, *, git_path: str | None = None):
    """seed 一条已 approved 的 SpecCandidate。"""
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    with session() as s:
        sc = SpecCandidate(
            slug=slug, title=f"T-{slug}",
            statement=statement, rationale="why",
            cluster_raw_ids_json=json.dumps([[1, 0]]),
            review_status="approved",
            git_path=git_path or f"specs/{slug}.md",
        )
        s.add(sc)
        s.flush()
        return sc.id


def test_draft_hits_approved_spec_parks_as_conflict(db, settings, llm_stub, make_raw):
    """draft_spec_from_cluster 遇到同 slug 且 approved → 不覆写, 挂 ConflictLog
    target_type='spec' + pending_payload_json, 等 owner 裁决。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate, L1Item

    sid = _make_approved_spec("locked", "OLD statement")

    # 准备 cluster: 2 条 decision
    rid_a = make_raw("raw a")
    rid_b = make_raw("raw b")
    for rid, idx in ((rid_a, 0), (rid_b, 0)):
        _seed_decision(rid, scene=f"S-{rid}", choice=f"C-{rid}")

    # 触发判据 (改动 4): 普适=true 让 1 条就触发, 2 条照样触发, 焦点在 approved-spec 防覆写
    llm_stub.set("spec_universal_check", '{"is_universal": true, "reason": "test"}')

    # LLM 给出和已批准 spec 同 slug 的新草稿
    llm_stub.set("ask", json.dumps({
        "slug": "locked",
        "title": "新草稿标题",
        "statement": "NEW statement",
        "rationale": "NEW rationale",
    }, ensure_ascii=False))

    result = draft_spec_from_cluster([(rid_a, 0), (rid_b, 0)])
    # 已批准 spec 不被覆写, 返的是旧那条 (id=sid)
    assert result is not None
    assert result.id == sid
    with session() as s:
        # 旧字段没动
        sc = s.get(SpecCandidate, sid)
        assert sc.statement == "OLD statement"
        assert sc.review_status == "approved"
        # 没有第二条 SpecCandidate
        rows = list(s.execute(select(SpecCandidate)).scalars())
        assert len(rows) == 1
        # ConflictLog 落了一条 + pending_payload 装好新内容
        conflicts = list(s.execute(
            select(ConflictLog).where(ConflictLog.target_slug == "locked")
        ).scalars())
        assert len(conflicts) == 1
        cl = conflicts[0]
        assert cl.target_type == "spec"
        assert cl.resolution == "open"
        payload = json.loads(cl.pending_payload_json)
        assert payload["statement"] == "NEW statement"
        assert payload["slug"] == "locked"


def test_resolve_approved_spec_superseded_overwrites_and_repromotes(
    db, settings, llm_stub, make_raw, monkeypatch,
):
    """采纳 → 用 pending_payload 覆盖 SpecCandidate 字段 + 触发 _repromote_spec。"""
    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate

    sid = _make_approved_spec("locked", "OLD")
    rid = make_raw("trigger")
    with session() as s:
        c = ConflictLog(
            raw_id=rid, target_type="spec", target_slug="locked",
            summary="approved-overwrite test", severity="medium",
            pending_payload_json=json.dumps({
                "slug": "locked", "title": "T",
                "statement": "NEW", "rationale": "RNEW",
                "keys": [[rid, 0]],
            }),
        )
        s.add(c)
        s.flush()
        log_id = c.id

    repromoted: list[str] = []
    def _fake_repromote(slug, *, reason):
        repromoted.append(slug)
    monkeypatch.setattr("helper.conflict.detector._repromote_spec", _fake_repromote)

    ok = resolve(log_id, resolution="superseded", resolver_domain="owner")
    assert ok is True
    with session() as s:
        sc = s.get(SpecCandidate, sid)
        # 字段被覆盖
        assert sc.statement == "NEW"
        assert sc.rationale == "RNEW"
        # 仍是 approved (没被打 superseded — 保留原状只更新内容)
        assert sc.review_status == "approved"
        assert sc.superseded_at is None
    assert repromoted == ["locked"]


def test_resolve_approved_spec_rejected_keeps_original(db, settings, llm_stub, make_raw):
    """保留 → 已批准 spec 完全不动, pending_payload 丢弃。"""
    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate

    sid = _make_approved_spec("locked", "ORIGINAL")
    rid = make_raw("trigger")
    with session() as s:
        c = ConflictLog(
            raw_id=rid, target_type="spec", target_slug="locked",
            summary="x", severity="medium",
            pending_payload_json=json.dumps({
                "slug": "locked", "title": "T",
                "statement": "WOULD-BE-NEW", "rationale": "...",
                "keys": [[rid, 0]],
            }),
        )
        s.add(c)
        s.flush()
        log_id = c.id

    ok = resolve(log_id, resolution="rejected", resolver_domain="owner")
    assert ok is True
    with session() as s:
        sc = s.get(SpecCandidate, sid)
        assert sc.statement == "ORIGINAL"
        # 不应再有第二条
        all_rows = list(s.execute(select(SpecCandidate)).scalars())
        assert len(all_rows) == 1


def test_resolve_approved_spec_coexist_spawns_v2(db, settings, llm_stub, make_raw):
    """都留 → pending_payload 落成 slug-v2 独立 SpecCandidate (pending review)。"""
    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog, SpecCandidate

    sid = _make_approved_spec("locked", "ORIGINAL")
    rid = make_raw("trigger")
    with session() as s:
        c = ConflictLog(
            raw_id=rid, target_type="spec", target_slug="locked",
            summary="x", severity="medium",
            pending_payload_json=json.dumps({
                "slug": "locked", "title": "新标题",
                "statement": "NEW", "rationale": "RNEW",
                "keys": [[rid, 0]],
            }),
        )
        s.add(c)
        s.flush()
        log_id = c.id

    ok = resolve(log_id, resolution="coexist", resolver_domain="owner")
    assert ok is True
    with session() as s:
        # 旧那条没动
        sc = s.get(SpecCandidate, sid)
        assert sc.statement == "ORIGINAL"
        # 新出一条 -v2 candidate, 状态 pending
        v2 = s.execute(
            select(SpecCandidate).where(SpecCandidate.slug == "locked-v2")
        ).scalar_one_or_none()
        assert v2 is not None
        assert v2.statement == "NEW"
        assert v2.review_status == "pending"
