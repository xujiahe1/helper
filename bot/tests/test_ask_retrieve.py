"""ask.retrieve — superseded 过滤(差集)+ 主路径召回。

验证点:
1. spec 候选 superseded 后,它独占的 raw 不出现在 retrieve raw 命中里
2. 同 raw 还撑着另一个 alive spec → raw 仍保留(差集策略防误伤)
3. raw_refs_json 三种格式 (([id, idx]) / [id] / ["id"]) 全部能解析
4. section atom 写入 fts 后能被 _fts_pass 召回
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


# ---------- _superseded_raw_ids 差集策略(spec 路径) ----------

def _make_spec(slug: str, statement: str, raw_refs: list, *, superseded: bool = False) -> int:
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    with session() as s:
        sc = SpecCandidate(
            slug=slug,
            title=slug,
            statement=statement,
            cluster_raw_ids_json=json.dumps(raw_refs),
            superseded_at=datetime.now(timezone.utc) if superseded else None,
        )
        s.add(sc)
        s.flush()
        return sc.id


def test_superseded_filters_orphan_raw(db, settings):
    """spec superseded,且它支撑的 raw 没被任何其它 alive spec 撑 → raw 进入 skip 集。"""
    from helper.ask.retrieve import _superseded_raw_ids

    _make_spec("s-old", "Helper 端口 8001", [[100, 0]], superseded=True)
    skip = _superseded_raw_ids()
    assert 100 in skip


def test_superseded_does_not_filter_shared_raw(db, settings):
    """同一条 raw 同时撑 superseded spec + alive spec → raw 不被过滤(差集)。"""
    from helper.ask.retrieve import _superseded_raw_ids

    _make_spec("s-old", "8001", [[200, 0]], superseded=True)
    _make_spec("s-new", "8009", [[200, 1]], superseded=False)
    skip = _superseded_raw_ids()
    assert 200 not in skip


# ---------- _fts_pass:section atom 召回 ----------

def test_fts_pass_picks_up_section(db, settings, make_raw):
    """L1Item.type=section 写完 fts 后能被 _fts_pass 召回。"""
    from helper.ask.retrieve import _fts_pass
    from helper.storage import fts, session
    from helper.storage.models import L1Item

    rid = make_raw("Helper 生产端口是 8009", source_type="cli")
    with session() as s:
        s.add(L1Item(
            raw_id=rid, idx=0, type="section",
            payload_json=json.dumps({
                "title": "端口", "body": "Helper 生产端口是 8009",
                "topics": ["端口"], "entities": ["Helper"],
            }, ensure_ascii=False),
        ))
    with session() as s:
        fts.index_l1_atom(s, rid, 0)

    hits = _fts_pass("Helper 生产端口", set())
    refs = {(h.type, h.ref) for h in hits}
    assert ("section", f"{rid}:0") in refs
