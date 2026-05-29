"""ask.retrieve — superseded 过滤(差集)+ 候选表直接召回。

验证点:
1. fact 候选 superseded 后,它独占的 raw 不出现在 retrieve raw 命中里
2. 同 raw 还撑着另一个 alive 候选,raw 仍保留(差集策略防误伤)
3. 未晋升的 fact 候选(mention=1)也能被 _candidate_pass 召回
4. bundle 与候选表同 (type, slug) 时去重
5. raw_refs_json 三种格式 (([id, idx]) / [id] / ["id"]) 全部能解析
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


# ---------- _parse_raw_refs ----------

def test_parse_raw_refs_three_formats():
    from helper.ask.retrieve import _parse_raw_refs

    assert _parse_raw_refs(json.dumps([[1, 0], [2, 1]])) == {1, 2}
    assert _parse_raw_refs(json.dumps([3, 4])) == {3, 4}
    assert _parse_raw_refs(json.dumps(["5", "6"])) == {5, 6}
    assert _parse_raw_refs("[]") == set()
    assert _parse_raw_refs(None) == set()
    assert _parse_raw_refs("not json") == set()


# ---------- _superseded_raw_ids 差集策略 ----------

def _make_fact(slug: str, subject: str, predicate: str, obj: str,
               raw_refs: list, *, superseded: bool = False) -> int:
    from helper.storage import session
    from helper.storage.models import FactCandidate

    with session() as s:
        fc = FactCandidate(
            slug=slug,
            statement=f"{subject} {predicate} {obj}",
            subject=subject,
            predicate=predicate,
            object=obj,
            raw_refs_json=json.dumps(raw_refs),
            mention_count=1,
            superseded_at=datetime.now(timezone.utc) if superseded else None,
        )
        s.add(fc)
        s.flush()
        return fc.id


def test_superseded_filters_orphan_raw(db, settings):
    """fact superseded,且它支撑的 raw 没被任何其它 alive 候选撑 → raw 进入 skip 集。"""
    from helper.ask.retrieve import _superseded_raw_ids

    _make_fact("f-old", "Helper", "端口", "8001", [[100, 0]], superseded=True)
    skip = _superseded_raw_ids()
    assert 100 in skip


def test_superseded_does_not_filter_shared_raw(db, settings):
    """同一条 raw 同时撑 superseded fact + alive fact → raw 不被过滤(差集)。"""
    from helper.ask.retrieve import _superseded_raw_ids

    _make_fact("f-old", "Helper", "端口", "8001", [[200, 0]], superseded=True)
    _make_fact("f-new", "Helper", "端口", "8009", [[200, 1]], superseded=False)
    skip = _superseded_raw_ids()
    assert 200 not in skip


def test_superseded_handles_spec_field_name(db, settings):
    """SpecCandidate 用 cluster_raw_ids_json,不是 raw_refs_json — 这次 hotfix 的回归测。"""
    from datetime import datetime, timezone

    from helper.ask.retrieve import _superseded_raw_ids
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    with session() as s:
        sc = SpecCandidate(
            slug="s-old",
            title="t",
            statement="一句话",
            cluster_raw_ids_json=json.dumps([[300, 0]]),
            superseded_at=datetime.now(timezone.utc),
        )
        s.add(sc)

    skip = _superseded_raw_ids()
    assert 300 in skip


# ---------- _candidate_pass:未晋升候选直接召回 ----------

def test_candidate_pass_picks_up_alive_fact(db, settings):
    """未晋升、未 superseded 的 fact 也能被 ask 召回。"""
    from helper.ask.retrieve import _candidate_pass

    _make_fact("f-port", "Helper", "生产端口", "8009", [[1, 0]], superseded=False)
    hits = _candidate_pass({"helper", "生产端口"})
    refs = {(h.type, h.ref) for h in hits}
    assert ("fact", "f-port") in refs


def test_candidate_pass_skips_superseded_fact(db, settings):
    """superseded fact 不进 _candidate_pass。"""
    from helper.ask.retrieve import _candidate_pass

    _make_fact("f-stale", "Helper", "生产端口", "8001", [[1, 0]], superseded=True)
    hits = _candidate_pass({"helper", "生产端口"})
    refs = {h.ref for h in hits}
    assert "f-stale" not in refs


def _make_entity(slug: str, name: str, description: str, *, superseded: bool = False) -> None:
    """造一个未晋升的 entity 候选。"""
    from helper.storage import session
    from helper.storage.models import EntityCandidate

    with session() as s:
        ec = EntityCandidate(
            slug=slug,
            name=name,
            description=description,
            mention_count=1,
            superseded_at=datetime.now(timezone.utc) if superseded else None,
        )
        s.add(ec)


def test_candidate_pass_picks_up_alive_entity(db, settings):
    """回归:未晋升、未 superseded 的 entity(concept)也要能被 ask 召回。

    历史 bug:_candidate_pass 漏扫 EntityCandidate,导致单文档抽出的 concept 类
    原子(如"加黑规则组")mention=1 进不了 bundle、候选路径又不扫,完全不可达。
    """
    from helper.ask.retrieve import _candidate_pass

    _make_entity("加黑规则组", "加黑规则组", "仅可配置主体不可见客体的规则组")
    hits = _candidate_pass({"加黑", "黑规", "规则", "则组"})
    refs = {(h.type, h.ref) for h in hits}
    assert ("entity", "加黑规则组") in refs


def test_candidate_pass_skips_superseded_entity(db, settings):
    """superseded entity 不进 _candidate_pass。"""
    from helper.ask.retrieve import _candidate_pass

    _make_entity("e-stale", "废弃概念", "已被替换的旧概念", superseded=True)
    hits = _candidate_pass({"废弃", "概念"})
    refs = {h.ref for h in hits}
    assert "e-stale" not in refs


# ---------- 集成: retrieve_relevant 整体行为 ----------

def test_retrieve_relevant_filters_old_value_after_supersede(db, settings, make_raw, tmp_path):
    """端到端:8001 fact 被 supersede 后,问端口只召回 8009。"""
    from helper.ask.retrieve import retrieve_relevant
    from helper.storage import session
    from helper.storage.models import L1Item, L1Result
    from helper.storage.spec_repo import init_spec_repo

    init_spec_repo(settings.helper_spec_git_dir)

    rid_old = make_raw("Helper 生产端口是 8001", source_type="cli")
    rid_new = make_raw("Helper 生产端口是 8009", source_type="cli")
    with session() as s:
        s.add(L1Result(raw_id=rid_old, error=""))
        s.add(L1Result(raw_id=rid_new, error=""))
        s.add(L1Item(
            raw_id=rid_old, idx=0, type="fact",
            payload_json=json.dumps({"subject": "Helper", "predicate": "生产端口", "object": "8001"}),
        ))
        s.add(L1Item(
            raw_id=rid_new, idx=0, type="fact",
            payload_json=json.dumps({"subject": "Helper", "predicate": "生产端口", "object": "8009"}),
        ))
    _make_fact("helper-port-8001", "Helper", "生产端口", "8001",
               [[rid_old, 0]], superseded=True)
    _make_fact("helper-port-8009", "Helper", "生产端口", "8009",
               [[rid_new, 0]], superseded=False)

    hits = retrieve_relevant("Helper 生产端口是多少", top_k=20)
    objects_seen = {(h.type, h.ref) for h in hits}
    # 新 fact 被召回
    assert ("fact", "helper-port-8009") in objects_seen
    # 旧 fact 不被召回
    assert ("fact", "helper-port-8001") not in objects_seen
    # 旧 raw 不在 raw 命中里
    raw_refs = {h.ref for h in hits if h.type == "raw"}
    assert str(rid_old) not in raw_refs
