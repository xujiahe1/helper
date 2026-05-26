"""知识化策略判定 API。代码侧只读策略不存阈值。"""

from __future__ import annotations

from helper.policy.loader import (
    Decay,
    DecayRule,
    EntityPromotion,
    EntityPromotionRule,
    KnowledgePolicy,
)


def _resolved_promotion(p: EntityPromotion, entity_type: str) -> EntityPromotionRule:
    """合并 by_type 与 default,返回该 entity_type 实际生效的规则。"""
    rule = p.by_type.get(entity_type)
    if rule is None:
        return p.default
    # 字段缺省时退到 default
    return EntityPromotionRule(
        min_raw_refs=rule.min_raw_refs if rule.min_raw_refs is not None else p.default.min_raw_refs,
        require_spec_relation=(
            rule.require_spec_relation
            if rule.require_spec_relation is not None
            else p.default.require_spec_relation
        ),
        promote=rule.promote,
    )


def should_promote(
    policy: KnowledgePolicy,
    entity_type: str,
    raw_ref_count: int,
    has_spec_relation: bool,
) -> bool:
    """判断 entity 是否应晋升为正式 MD。"""
    rule = _resolved_promotion(policy.entity_promotion, entity_type)
    if rule.promote == "never":
        return False
    if rule.min_raw_refs is not None and raw_ref_count < rule.min_raw_refs:
        return False
    if rule.require_spec_relation and not has_spec_relation:
        return False
    return True


def _resolved_decay(d: Decay, entity_type: str) -> DecayRule:
    rule = d.by_type.get(entity_type)
    if rule is None:
        return d.default
    return DecayRule(
        months=rule.months if rule.months is not None else d.default.months,
        action=rule.action if rule.action is not None else d.default.action,
    )


def should_decay(
    policy: KnowledgePolicy,
    entity_type: str,
    months_since_last_ref: int,
) -> tuple[bool, str | None]:
    """判断 entity 是否应衰减。返回 (是否衰减, action)。"""
    rule = _resolved_decay(policy.decay, entity_type)
    if rule.action == "never" or rule.months is None:
        return False, None
    if months_since_last_ref < rule.months:
        return False, None
    return True, rule.action


def should_merge(policy: KnowledgePolicy, similarity: float) -> bool:
    """语义相似度是否达到合并阈值(达到再交给 judge_model 做最终判断)。"""
    return similarity >= policy.merge.semantic_similarity_threshold
