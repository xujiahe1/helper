"""specgen.draft 触发判据 (改动 4) — 普适 / 饱和 / 静默期 三分支。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select


def _seed_decision(raw_id: int, idx: int = 0, scene: str = "S", choice: str = "C") -> None:
    from helper.storage import session
    from helper.storage.models import L1Item

    with session() as s:
        s.add(L1Item(
            raw_id=raw_id,
            idx=idx,
            type="decision",
            payload_json=json.dumps({
                "scene": scene,
                "signals": ["a"],
                "tradeoffs": [],
                "choice": choice,
                "rationale": "R",
            }, ensure_ascii=False),
        ))


def _stub_draft_llm(llm_stub) -> None:
    """给 ask task 一个稳定的 spec JSON, 让触发后 draft 能成功落库。"""
    llm_stub.set("ask", json.dumps({
        "slug": "test_spec",
        "title": "T",
        "statement": "在 X 场景下应该 Y",
        "rationale": "因为 Z",
    }, ensure_ascii=False))


def _set_raw_age_days(raw_id: int, days: int) -> None:
    """把 raw.created_at 调成 days 天前 (UTC)。"""
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        r = s.get(RawInput, raw_id)
        assert r is not None
        r.created_at = datetime.now(timezone.utc) - timedelta(days=days)


def test_universal_one_decision_triggers(db, settings, llm_stub, make_raw):
    """LLM 判 is_universal=true → 1 条 decision 即触发 draft, 落 SpecCandidate。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    rid = make_raw("以后 X 类问题统一这样处理")
    _seed_decision(rid)

    llm_stub.set("spec_universal_check", '{"is_universal": true, "reason": "明确说以后都"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster([(rid, 0)])
    assert sc is not None
    assert sc.slug == "test_spec"

    with session() as s:
        rows = s.execute(select(SpecCandidate)).scalars().all()
        assert len(rows) == 1


def test_non_universal_below_three_no_trigger(db, settings, llm_stub, make_raw):
    """is_universal=false + 2 条 decision + 不在静默期 → return None, 不落 SpecCandidate。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    rid_a = make_raw("这次先这样")
    rid_b = make_raw("这一次也这样")
    _seed_decision(rid_a)
    _seed_decision(rid_b)
    # 默认 created_at = now → 不静默

    llm_stub.set("spec_universal_check", '{"is_universal": false, "reason": "单次"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster([(rid_a, 0), (rid_b, 0)])
    assert sc is None

    with session() as s:
        assert s.execute(select(SpecCandidate)).scalars().all() == []


def test_saturation_three_triggers_even_if_not_universal(db, settings, llm_stub, make_raw):
    """is_universal=false + ≥ 3 条 → 数量饱和兜底触发。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    keys = []
    for i in range(3):
        rid = make_raw(f"r{i}")
        _seed_decision(rid)
        keys.append((rid, 0))

    llm_stub.set("spec_universal_check", '{"is_universal": false, "reason": "single"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster(keys)
    assert sc is not None

    with session() as s:
        rows = s.execute(select(SpecCandidate)).scalars().all()
        assert len(rows) == 1


def test_silent_90d_triggers_even_if_below_three(db, settings, llm_stub, make_raw):
    """is_universal=false + 1 条 + raw 距今 91 天 + 簇没 promote 过 → 静默触发。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    rid = make_raw("旧 raw")
    _seed_decision(rid)
    _set_raw_age_days(rid, 91)

    llm_stub.set("spec_universal_check", '{"is_universal": false, "reason": "single"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster([(rid, 0)])
    assert sc is not None

    with session() as s:
        rows = s.execute(select(SpecCandidate)).scalars().all()
        assert len(rows) == 1


def test_within_silence_window_no_trigger(db, settings, llm_stub, make_raw):
    """is_universal=false + 1 条 + raw 距今 30 天 (< 90) → 不触发。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    rid = make_raw("还不够老")
    _seed_decision(rid)
    _set_raw_age_days(rid, 30)

    llm_stub.set("spec_universal_check", '{"is_universal": false, "reason": "single"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster([(rid, 0)])
    assert sc is None

    with session() as s:
        assert s.execute(select(SpecCandidate)).scalars().all() == []


# ─────────────────────────────────────────────────────────────────────────────
# 改动 3: SpecTopic 语义聚类
# ─────────────────────────────────────────────────────────────────────────────


def _stub_decision_embedding(monkeypatch, vector_map: dict[str, list[float]]):
    """patch _decision_embedding 直接按 payload.choice 查表返 fp16 bytes。"""
    import struct

    def fake(payload: dict) -> bytes:
        vec = vector_map.get(payload.get("choice", ""))
        if vec is None or len(vec) != 1024:
            return b""
        return struct.pack(f"{len(vec)}e", *vec)

    monkeypatch.setattr("helper.ingest.sink._decision_embedding", fake)


def _vec(seed: float) -> list[float]:
    return [seed] * 1024


def _vec_split(a: float, b: float, split: int = 512) -> list[float]:
    """前 split 维 a, 后段 b — 跟 _vec(a)/_vec(b) 余弦可控。"""
    return [a] * split + [b] * (1024 - split)


def _seed_decision_with_embedding(
    raw_id: int, idx: int, choice: str, vector: list[float],
) -> None:
    """直接落 L1Item, 模拟 sink 已经算好 embedding 的场景。"""
    import struct

    from helper.storage import session
    from helper.storage.models import L1Item

    blob = struct.pack(f"{len(vector)}e", *vector)
    with session() as s:
        s.add(L1Item(
            raw_id=raw_id,
            idx=idx,
            type="decision",
            payload_json=json.dumps({
                "scene": "S", "signals": [], "tradeoffs": [],
                "choice": choice, "rationale": "R",
            }, ensure_ascii=False),
            embedding=blob,
        ))


def test_assign_topic_creates_new_when_below_threshold(db, settings, make_raw):
    """两条 decision 余弦正交 (= 0) → 落两个 SpecTopic, 各 count=1。"""
    from helper.specgen.cluster import assign_topic
    from helper.storage import session
    from helper.storage.models import L1Item, SpecTopic

    rid_a = make_raw("ra")
    rid_b = make_raw("rb")
    _seed_decision_with_embedding(rid_a, 0, "ca", _vec(1.0))
    _seed_decision_with_embedding(rid_b, 0, "cb", _vec_split(1.0, -1.0, 512))

    tid_a = assign_topic(rid_a, 0)
    tid_b = assign_topic(rid_b, 0)
    assert tid_a is not None and tid_b is not None
    assert tid_a != tid_b

    with session() as s:
        topics = list(s.execute(select(SpecTopic)).scalars())
        assert len(topics) == 2
        assert all(t.decision_count == 1 for t in topics)
        items = list(s.execute(select(L1Item)).scalars())
        assert {it.topic_id for it in items} == {tid_a, tid_b}


def test_assign_topic_merges_above_threshold(db, settings, make_raw):
    """两条 decision 同向 (常向量, 余弦 = 1.0) → 同 topic, count=2, centroid 仍同向。"""
    from helper.specgen.cluster import assign_topic
    from helper.storage import session
    from helper.storage.models import SpecTopic

    rid_a = make_raw("ra")
    rid_b = make_raw("rb")
    _seed_decision_with_embedding(rid_a, 0, "ca", _vec(0.5))
    _seed_decision_with_embedding(rid_b, 0, "cb", _vec(0.5))

    tid_a = assign_topic(rid_a, 0)
    tid_b = assign_topic(rid_b, 0)
    assert tid_a == tid_b

    with session() as s:
        topics = list(s.execute(select(SpecTopic)).scalars())
        assert len(topics) == 1
        assert topics[0].decision_count == 2


def test_scan_finds_saturated_topic(db, settings, make_raw):
    """3 条 decision 同向归同 topic → scan_topics_for_draft 返回它。"""
    from helper.specgen.cluster import assign_topic, scan_topics_for_draft

    keys = []
    for i in range(3):
        rid = make_raw(f"r{i}")
        _seed_decision_with_embedding(rid, 0, f"c{i}", _vec(0.5))
        assert assign_topic(rid, 0) is not None
        keys.append((rid, 0))

    due = scan_topics_for_draft()
    assert len(due) == 1


def test_scan_skips_promoted_topic(db, settings, make_raw):
    """topic 已 promote 过且未过 30 天冷却 → scan 不再返回它,
    即便 decision_count >= 3 也跳过。"""
    from datetime import datetime, timezone

    from helper.specgen.cluster import assign_topic, scan_topics_for_draft
    from helper.storage import session
    from helper.storage.models import SpecTopic

    for i in range(3):
        rid = make_raw(f"r{i}")
        _seed_decision_with_embedding(rid, 0, f"c{i}", _vec(0.5))
        assign_topic(rid, 0)

    # 模拟刚 promote 完
    with session() as s:
        topic = s.execute(select(SpecTopic)).scalar_one()
        topic.last_promoted_at = datetime.now(timezone.utc)

    due = scan_topics_for_draft()
    assert due == []


def test_silent_skipped_if_already_drafted(db, settings, llm_stub, make_raw):
    """老 cluster 但已被某 SpecCandidate 引用过 (无论 superseded 与否) → 不再静默触发,
    避免反复打扰 owner。"""
    from helper.specgen.draft import draft_spec_from_cluster
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    rid = make_raw("老 raw 但已 promote 过")
    _seed_decision(rid)
    _set_raw_age_days(rid, 120)

    # 预 seed 一条 SpecCandidate 引用 (rid, 0)
    with session() as s:
        s.add(SpecCandidate(
            slug="prior",
            title="prior",
            statement="x",
            rationale="y",
            cluster_raw_ids_json=json.dumps([[rid, 0]]),
        ))

    llm_stub.set("spec_universal_check", '{"is_universal": false, "reason": "single"}')
    _stub_draft_llm(llm_stub)

    sc = draft_spec_from_cluster([(rid, 0)])
    assert sc is None

    with session() as s:
        # 仍只有那条 prior, 没新增
        rows = s.execute(select(SpecCandidate)).scalars().all()
        assert len(rows) == 1
        assert rows[0].slug == "prior"
