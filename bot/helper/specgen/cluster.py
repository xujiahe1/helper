"""L1 聚类 — 同 entity 共现 + scene 关键词重叠的 L1 聚成一簇。

M1 实现:朴素 — 共享 ≥1 个 entity slug 即同簇,后续 M3 再换 embedding。
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import EntityCandidate, L1Result


def _entities_for_raw(raw_id: int, ec_by_slug: dict) -> set[str]:
    return {slug for slug, ec in ec_by_slug.items() if raw_id in json.loads(ec.raw_refs_json or "[]")}


def cluster_l1_results(*, min_cluster_size: int = 2) -> list[list[int]]:
    """返回 raw_id 列表的列表(每个内列表 = 一簇)。"""
    with session() as s:
        l1_rows = s.execute(
            select(L1Result.raw_id).where(L1Result.error == "")
        ).scalars().all()
        ecs = s.execute(select(EntityCandidate)).scalars().all()
        ec_by_slug = {ec.slug: ec for ec in ecs}

        # raw_id → entity slug set
        raw_to_entities = {rid: _entities_for_raw(rid, ec_by_slug) for rid in l1_rows}

    # Union-Find
    parent: dict[int, int] = {rid: rid for rid in raw_to_entities}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # 同 entity → 同簇
    by_entity: dict[str, list[int]] = defaultdict(list)
    for rid, ents in raw_to_entities.items():
        for e in ents:
            by_entity[e].append(rid)
    for raws in by_entity.values():
        for i in range(1, len(raws)):
            union(raws[0], raws[i])

    groups: dict[int, list[int]] = defaultdict(list)
    for rid in raw_to_entities:
        groups[find(rid)].append(rid)

    return sorted(
        [sorted(v) for v in groups.values() if len(v) >= min_cluster_size],
        key=lambda g: -len(g),
    )
