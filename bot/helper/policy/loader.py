"""加载策略 yaml(知识化策略 + LLM routing)。

bot 运行时**只从 spec git repo 读策略**;包内打包的 default 仅作首次 seed。
设计见 docs/architecture.md §8.6。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# ────────────────────────────────────────────────────────────────
# Knowledge policy(entity 晋升 / decay / 合并阈值)
# ────────────────────────────────────────────────────────────────


class EntityPromotionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_raw_refs: int | None = None
    require_spec_relation: bool | None = None
    promote: Literal["never"] | None = None  # 整类禁止晋升


class EntityPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: EntityPromotionRule
    by_type: dict[str, EntityPromotionRule] = Field(default_factory=dict)


class DecayRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    months: int | None = None
    action: Literal["deprioritize", "delete", "never"] | None = None


class Decay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: DecayRule
    by_type: dict[str, DecayRule] = Field(default_factory=dict)


class Merge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_similarity_threshold: float
    judge_model: str


class KnowledgePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    entity_promotion: EntityPromotion
    decay: Decay
    merge: Merge


# ────────────────────────────────────────────────────────────────
# LLM routing(task → model + provider)
# ────────────────────────────────────────────────────────────────


class TaskRouting(BaseModel):
    # protected_namespaces=() 关掉 pydantic 对 model_ 前缀的警告
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str
    provider: Literal["anthropic", "openai"]
    max_tokens: int | None = None


class LlmRoutingDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = 4096


class LlmRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    defaults: LlmRoutingDefaults = Field(default_factory=LlmRoutingDefaults)
    tasks: dict[str, TaskRouting]


# ────────────────────────────────────────────────────────────────
# Loader 实现
# ────────────────────────────────────────────────────────────────

_DEFAULTS_PACKAGE = "helper.policy.defaults"

KNOWLEDGE_POLICY_FILENAME = "knowledge_policy.yaml"
LLM_ROUTING_FILENAME = "llm_routing.yaml"
TOPIC_ACL_FILENAME = "topic_acl.yaml"

# Spec repo 内的相对路径
KNOWLEDGE_POLICY_RELPATH = Path("meta") / "policies" / KNOWLEDGE_POLICY_FILENAME
LLM_ROUTING_RELPATH = Path("meta") / "policies" / LLM_ROUTING_FILENAME
TOPIC_ACL_RELPATH = Path("meta") / "policies" / TOPIC_ACL_FILENAME


def default_policy_text(filename: str) -> str:
    """读 bot 内打包的某个默认策略 yaml 文本。"""
    return (files(_DEFAULTS_PACKAGE) / filename).read_text(encoding="utf-8")


def all_default_filenames() -> list[str]:
    """所有 defaults/ 下的 yaml 文件名,供 spec_repo init seed 用。"""
    return sorted(
        f.name for f in files(_DEFAULTS_PACKAGE).iterdir() if f.name.endswith(".yaml")
    )


def load_knowledge_policy(spec_repo_dir: Path) -> KnowledgePolicy:
    f = spec_repo_dir / KNOWLEDGE_POLICY_RELPATH
    text = f.read_text(encoding="utf-8") if f.exists() else default_policy_text(KNOWLEDGE_POLICY_FILENAME)
    return KnowledgePolicy.model_validate(yaml.safe_load(text))


def load_llm_routing(spec_repo_dir: Path) -> LlmRouting:
    f = spec_repo_dir / LLM_ROUTING_RELPATH
    text = f.read_text(encoding="utf-8") if f.exists() else default_policy_text(LLM_ROUTING_FILENAME)
    return LlmRouting.model_validate(yaml.safe_load(text))


# ────────────────────────────────────────────────────────────────
# Topic ACL — 内容级访问控制(M8)
# ────────────────────────────────────────────────────────────────


class TopicAclEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    owner_domain: str
    allowed_domains: list[str] = Field(default_factory=list)
    deny_response: str = "这个话题我不知道。"


class TopicAcl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    default_on_uncertain: str = ""  # 空串 = 不打标(对所有人可见);非空 = topic_id
    topics: list[TopicAclEntry] = Field(default_factory=list)

    def by_id(self, topic_id: str) -> TopicAclEntry | None:
        for t in self.topics:
            if t.id == topic_id:
                return t
        return None

    def is_allowed(self, asker_domain: str, topic_id: str) -> bool:
        """asker 是否被允许看到带 topic_id 标的内容。空 topic = 公开 = 允许。"""
        if not topic_id:
            return True
        entry = self.by_id(topic_id)
        if entry is None:
            # 标了但 yaml 里没这条 topic — 保守起见拒(yaml 改名 / 删 topic 后存量数据兜底)
            return False
        return asker_domain in entry.allowed_domains


def load_topic_acl(spec_repo_dir: Path) -> TopicAcl:
    f = spec_repo_dir / TOPIC_ACL_RELPATH
    text = f.read_text(encoding="utf-8") if f.exists() else default_policy_text(TOPIC_ACL_FILENAME)
    return TopicAcl.model_validate(yaml.safe_load(text))
