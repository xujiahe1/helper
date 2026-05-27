"""追问 Engine — M2 灵魂模块(LLM 驱动版)。

策略 yaml: meta/policies/inquiry_strategies.yaml(seed 自 helper/policy/defaults/)。
触发: raw 落库 + L1 抽完后,sink._run_consumers 末尾调 generate_inquiries(raw_id),
      LLM 看 raw + decision L1Item + 上下文 raw + 策略目录,自己挑 0-3 条命中并写问题。
命中率打标: 用户回答 → record_answer 关联 + mark_hit 标记。

不主动 push,等 Inbox 周报(M2-2)统一打包推。
"""

from helper.inquiry.engine import (
    InquiryHit,
    evaluate_for_raw,
    generate_inquiries,
    load_strategies,
    mark_hit,
    record_answer,
)

__all__ = [
    "InquiryHit",
    "evaluate_for_raw",
    "generate_inquiries",
    "load_strategies",
    "mark_hit",
    "record_answer",
]
