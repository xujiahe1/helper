"""L1 决策原子聚类 — 基于 entity 共现把 L1Item.type=decision 聚成簇。

集合元素: (raw_id, idx) — 一条 raw 可以贡献多条 decision。
聚类规则: 两个 decision 共享至少 1 个 entity 即同簇。

entity 来源:
- section.payload.entities[]  ← v2 prompt 直接给的
- decision.payload.subject / scene / scope / entity_a / entity_b ← 从 decision 字段里抽

桥接维度是 raw_id: entity 出现在 raw_id=X 里 → 同一个 entity 触达 raw=X 的所有 decision。
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import L1Item


_DECISION_ENTITY_FIELDS = ("subject", "scene", "scope", "entity_a", "entity_b")


def cluster_l1_results(*, min_cluster_size: int = 2) -> list[list[tuple[int, int]]]:
    """返回 (raw_id, idx) 元组列表的列表(每个内列表 = 一簇 decision)。"""
    with session() as s:
        all_items = list(s.execute(
            select(L1Item).where(L1Item.type.in_(["section", "decision"]))
        ).scalars())

    decision_keys: list[tuple[int, int]] = []
    raw_to_decisions: dict[int, list[tuple[int, int]]] = defaultdict(list)
    entity_to_raws: dict[str, set[int]] = defaultdict(set)

    for it in all_items:
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if it.type == "decision":
            key = (it.raw_id, it.idx)
            decision_keys.append(key)
            raw_to_decisions[it.raw_id].append(key)
            for k in _DECISION_ENTITY_FIELDS:
                v = payload.get(k)
                if isinstance(v, str) and v.strip():
                    entity_to_raws[v.strip()].add(it.raw_id)
        elif it.type == "section":
            ents = payload.get("entities") or []
            if isinstance(ents, list):
                for e in ents:
                    if isinstance(e, str) and e.strip():
                        entity_to_raws[e.strip()].add(it.raw_id)

    if not decision_keys:
        return []

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

    for raws in entity_to_raws.values():
        if len(raws) < 2:
            continue
        keys: list[tuple[int, int]] = []
        for r in raws:
            keys.extend(raw_to_decisions.get(r, []))
        for i in range(1, len(keys)):
            union(keys[0], keys[i])

    groups: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for k in decision_keys:
        groups[find(k)].append(k)

    return sorted(
        [sorted(v) for v in groups.values() if len(v) >= min_cluster_size],
        key=lambda g: -len(g),
    )
