"""策略 — 外置 yaml + 判定 API。M1 包含 knowledge / llm_routing;M8 加 topic_acl。"""

from helper.policy.knowledge import should_decay, should_merge, should_promote
from helper.policy.loader import (
    KNOWLEDGE_POLICY_RELPATH,
    LLM_ROUTING_RELPATH,
    TOPIC_ACL_RELPATH,
    KnowledgePolicy,
    LlmRouting,
    TaskRouting,
    TopicAcl,
    TopicAclEntry,
    all_default_filenames,
    default_policy_text,
    load_knowledge_policy,
    load_llm_routing,
    load_topic_acl,
)

__all__ = [
    "KNOWLEDGE_POLICY_RELPATH",
    "LLM_ROUTING_RELPATH",
    "TOPIC_ACL_RELPATH",
    "KnowledgePolicy",
    "LlmRouting",
    "TaskRouting",
    "TopicAcl",
    "TopicAclEntry",
    "all_default_filenames",
    "default_policy_text",
    "load_knowledge_policy",
    "load_llm_routing",
    "load_topic_acl",
    "should_decay",
    "should_merge",
    "should_promote",
]
