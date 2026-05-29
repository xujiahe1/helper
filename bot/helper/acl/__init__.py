"""Topic ACL — 内容级访问控制(M8)。

非白名单用户问到带 topic 标的内容时, retrieve 出口直接过滤 + ask 入口短路返
deny_response, LLM 看不到敏感原文 — 比 memory 层硬, LLM 推翻不了。

入口:
- current_acl(): 加载 topic_acl.yaml, 进程内 cache, 改 yaml 后 reset_acl_cache 清。
- tag_text(text): LLM 判这段文本应贴哪个 topic; 失败 → 用 yaml 的 default_on_uncertain。
- tag_raw(raw_id): 给 raw 落标 + 同步派生 atom。
- backfill_all(): 一次性扫所有未打标的 raw。
- filter_hits(asker, hits): retrieve 出口过滤(不可见的全删)。
- deny_for_question(asker, question, chat_context): ask 入口短路判定;命中返 deny_response, 否则返 None。
"""

from helper.acl.policy import (
    current_acl,
    deny_for_question,
    filter_hits,
    is_allowed,
    reset_acl_cache,
)
from helper.acl.tagger import backfill_all, tag_raw, tag_text

__all__ = [
    "backfill_all",
    "current_acl",
    "deny_for_question",
    "filter_hits",
    "is_allowed",
    "reset_acl_cache",
    "tag_raw",
    "tag_text",
]
