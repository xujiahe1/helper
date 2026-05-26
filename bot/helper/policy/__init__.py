"""策略 — 外置 yaml + 判定 API。M1 包含 knowledge / llm_routing 两套。"""

from helper.policy.knowledge import should_decay, should_merge, should_promote
from helper.policy.loader import (
    KNOWLEDGE_POLICY_RELPATH,
    LLM_ROUTING_RELPATH,
    KnowledgePolicy,
    LlmRouting,
    TaskRouting,
    all_default_filenames,
    default_policy_text,
    load_knowledge_policy,
    load_llm_routing,
)

__all__ = [
    "KNOWLEDGE_POLICY_RELPATH",
    "LLM_ROUTING_RELPATH",
    "KnowledgePolicy",
    "LlmRouting",
    "TaskRouting",
    "all_default_filenames",
    "default_policy_text",
    "load_knowledge_policy",
    "load_llm_routing",
    "should_decay",
    "should_merge",
    "should_promote",
]
