"""Ontology — entity 涌现 + 晋升。

L1 signals → entity_candidates(sqlite),阈值达标晋升到 git ontology/entities/<slug>.md。
策略走 meta/policies/knowledge_policy.yaml。
"""

from helper.ontology.extractor import extract_from_l1, extract_from_text
from helper.ontology.maintenance import run_maintenance
from helper.ontology.promoter import promote_eligible, promote_one

__all__ = [
    "extract_from_l1",
    "extract_from_text",
    "promote_eligible",
    "promote_one",
    "run_maintenance",
]
