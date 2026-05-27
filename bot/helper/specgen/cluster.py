"""L1 决策原子聚类 — 基于 entity 共现把 L1Item.type=decision 聚成簇。

集合元素: (raw_id, idx) — 一条 raw 可以贡献多条 decision。
M1 实现: 共享 ≥1 个 entity slug 即同簇。entity_candidates.raw_refs_json 已是
[[raw_id, idx], ...] 格式 — 这里把每条 decision item 的 (raw_id, idx) 与 entity
ref 列表做交集即可。
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import EntityCandidate, L1Item


def _ref_tuples(raw_refs_json: str) -> set[tuple[int, int]]:
    """raw_refs_json → {(raw_id, idx), ...}。容忍老数据 [raw_id] 形式 → 当 idx=0。"""
    out: set[tuple[int, int]] = set()
    for r in json.loads(raw_refs_json or "[]"):
        if isinstance(r, list) and len(r) == 2:
            out.add((int(r[0]), int(r[1])))
        elif isinstance(r, int):
            out.add((r, 0))
    return out


def cluster_l1_results(*, min_cluster_size: int = 2) -> list[list[tuple[int, int]]]:
    """返回 (raw_id, idx) 元组列表的列表(每个内列表 = 一簇 decision)。"""
    with session() as s:
        decision_keys = [
            (it.raw_id, it.idx)
            for it in s.execute(
                select(L1Item).where(L1Item.type == "decision")
            ).scalars()
        ]
        ecs = s.execute(select(EntityCandidate)).scalars().all()
        ec_refs_by_slug = {ec.slug: _ref_tuples(ec.raw_refs_json) for ec in ecs}

    if not decision_keys:
        return []

    # decision_key → entity slugs
    key_to_entities: dict[tuple[int, int], set[str]] = defaultdict(set)
    for slug, refs in ec_refs_by_slug.items():
        for k in refs:
            if k in set(decision_keys):
                key_to_entities[k].add(slug)

    parent: dict[tuple[int, int], tuple[int, int]] = {k: k for k in decision_keys}

    def find(x: tuple[int, int]) -> tuple[int, int]:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_entity: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for k, ents in key_to_entities.items():
        for e in ents:
            by_entity[e].append(k)
    for keys in by_entity.values():
        for i in range(1, len(keys)):
            union(keys[0], keys[i])

    groups: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for k in decision_keys:
        groups[find(k)].append(k)

    return sorted(
        [sorted(v) for v in groups.values() if len(v) >= min_cluster_size],
        key=lambda g: -len(g),
    )
