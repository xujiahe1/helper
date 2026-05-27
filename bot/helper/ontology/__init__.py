"""Ontology — entity (concept) + relation 涌现 + 晋升。

L1Item.type=concept → entity_candidates(sqlite),阈值达标晋升到 git ontology/entities/<slug>.md。
L1Item.type=relation → relation_candidates,阈值达标晋升到 git ontology/relationships/<slug>.md。
策略走 meta/policies/knowledge_policy.yaml(entities)+ relations.py 内置阈值(relations)。
"""

from helper.ontology.extractor import consume_concept_items
from helper.ontology.maintenance import run_maintenance
from helper.ontology.promoter import promote_eligible, promote_one
from helper.ontology.relations import consume_relation_items
from helper.ontology.relations import promote_eligible as promote_eligible_relations
from helper.ontology.relations import promote_one as promote_one_relation

__all__ = [
    "consume_concept_items",
    "consume_relation_items",
    "promote_eligible",
    "promote_eligible_relations",
    "promote_one",
    "promote_one_relation",
    "run_maintenance",
]
