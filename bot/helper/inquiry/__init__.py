"""追问 Engine v0 — M2 灵魂模块。

策略 yaml 在 meta/policies/inquiry_strategies.yaml(seed 自 helper/policy/defaults/)。
触发: raw + L1 落库后 evaluate(raw_id) 扫策略,命中即出问题落 inquiry_log。
命中率打标: 用户回答另起 raw,answer_raw_id 回填,LLM judge 是否答到点 → hit。
"""

from helper.inquiry.engine import (
    InquiryHit,
    evaluate_for_raw,
    load_strategies,
    mark_hit,
    record_answer,
)

__all__ = [
    "InquiryHit",
    "evaluate_for_raw",
    "load_strategies",
    "mark_hit",
    "record_answer",
]
