"""Ingest pipeline — L0 raw → L1(typed knowledge atoms)→ 4 类候选 → 晋升。

L1 抽取出 0..N 条原子(decision/fact/case/concept/relation),sink.process_raw
后串接 4 个 consumer 把 concept/relation/fact/case 收口到候选表;decision 留给
specgen.cluster 聚类 + draft。
"""

from helper.ingest.l1_structure import L1Item, L1Output, structure
from helper.ingest.sink import backfill_pending, process_raw, schedule_l1

__all__ = [
    "L1Item",
    "L1Output",
    "backfill_pending",
    "process_raw",
    "schedule_l1",
    "structure",
]
