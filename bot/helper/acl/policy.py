"""ACL 加载 / 出口过滤 / 入口判定。"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from helper.config import get_settings
from helper.policy import TopicAcl, load_topic_acl

if TYPE_CHECKING:
    from helper.ask.retrieve import Hit

log = logging.getLogger(__name__)


@lru_cache
def current_acl() -> TopicAcl:
    """从 spec repo 读 topic_acl.yaml,进程内 cache。"""
    s = get_settings()
    return load_topic_acl(s.helper_spec_git_dir)


def reset_acl_cache() -> None:
    """测试 / yaml 热改后清缓存。"""
    current_acl.cache_clear()


def is_allowed(asker_domain: str, topic_id: str) -> bool:
    """asker 是否有权看到带 topic_id 标的内容。空 topic = 公开 = 允许。"""
    return current_acl().is_allowed(asker_domain, topic_id)


# ────────────────────────────────────────────────────────────────
# retrieve 出口过滤
# ────────────────────────────────────────────────────────────────


def filter_hits(asker_domain: str, hits: list["Hit"]) -> tuple[list["Hit"], list["Hit"]]:
    """把 hits 按 asker 是否有权可见拆成 (allowed, blocked)。

    Hit 自身不带 acl_topic_id 字段(retrieve 三路融合时不一定存)。这里反查每条 hit
    对应表的 acl_topic_id 列做判定。Bundle hit (path A jaccard) 用 sources='jaccard'
    标记;为它们查 SpecCandidate / EntityCandidate。
    """
    if not hits:
        return [], []
    acl = current_acl()
    if not acl.topics:
        return list(hits), []
    topic_map = _resolve_hit_topics(hits)
    allowed: list[Hit] = []
    blocked: list[Hit] = []
    for h in hits:
        topic_id = topic_map.get((h.type, h.ref), "")
        if acl.is_allowed(asker_domain, topic_id):
            allowed.append(h)
        else:
            blocked.append(h)
    return allowed, blocked


def _resolve_hit_topics(hits: list["Hit"]) -> dict[tuple[str, str], str]:
    """批量查 (type, ref) → acl_topic_id。失败任一项默认空(公开)。"""
    from sqlalchemy import select

    from helper.storage import session
    from helper.storage.models import (
        CaseCandidate,
        EntityCandidate,
        FactCandidate,
        RawInput,
        RelationCandidate,
    )

    by_type: dict[str, list[str]] = {}
    for h in hits:
        by_type.setdefault(h.type, []).append(h.ref)

    out: dict[tuple[str, str], str] = {}
    with session() as s:
        if "raw" in by_type:
            ids = [int(r) for r in by_type["raw"] if r.isdigit()]
            if ids:
                rows = s.execute(
                    select(RawInput.id, RawInput.acl_topic_id).where(RawInput.id.in_(ids))
                ).all()
                for rid, tid in rows:
                    out[("raw", str(rid))] = tid or ""
        for kind, model in (
            ("entity", EntityCandidate),
            ("fact", FactCandidate),
            ("case", CaseCandidate),
            ("relation", RelationCandidate),
        ):
            if kind not in by_type:
                continue
            slugs = by_type[kind]
            rows = s.execute(
                select(model.slug, model.acl_topic_id).where(model.slug.in_(slugs))
            ).all()
            for slug, tid in rows:
                out[(kind, slug)] = tid or ""
        # spec 暂不打 ACL — bundle 里的 spec 是已晋升的"公开决策规约",不应在 ACL 范围内。
        # 如果未来需要给 spec 打标,在这里加 SpecCandidate 反查即可。

    return out


# ────────────────────────────────────────────────────────────────
# ask 入口短路判定
# ────────────────────────────────────────────────────────────────


def scrub_output(asker_domain: str, text: str) -> str | None:
    """出口侧硬过滤。

    遍历所有 topic, 若 asker 不在该 topic.allowed_domains 且 text 里出现任一
    output_blocklist_terms → 返该 topic.deny_response(整段替换)。

    返 None 表示无命中, 调用方应保留原 text。

    设计: 兜底防 LLM 即便没拿到敏感原文, 仍凭参数知识 / 迂回话术自己脑补出敏感
    名字。整段替换比"打码"更安全 — 上下文也可能透露身份。
    """
    if not text:
        return None
    acl = current_acl()
    for entry in acl.topics:
        if asker_domain in entry.allowed_domains:
            continue
        if not entry.output_blocklist_terms:
            continue
        for term in entry.output_blocklist_terms:
            if term and term in text:
                log.info(
                    "scrub_output hit topic=%s term=%r asker=%s",
                    entry.id, term, asker_domain,
                )
                return entry.deny_response
    return None


def deny_for_question(
    asker_domain: str, question: str, chat_context: str = ""
) -> str | None:
    """问题文本 + 历史上下文跑一次 acl_tag, 命中且 asker 非白名单 → 返 deny_response。

    返 None 表示不拦, 让 ask 主路径继续。

    设计: 防"新内容还没 ingest 时仍泄"或"问题本身敏感但检索没召回"两种漏点。
    """
    acl = current_acl()
    if not acl.topics:
        return None

    from helper.acl.tagger import tag_text

    text = question.strip()
    if chat_context:
        text = f"{chat_context}\n\n# 当前提问\n{question}"
    topic_id = tag_text(text)
    if not topic_id:
        return None
    if acl.is_allowed(asker_domain, topic_id):
        return None
    entry = acl.by_id(topic_id)
    if entry is None:
        # 标了未知 topic — 兜底拒
        log.warning("deny_for_question: tagged unknown topic_id=%s", topic_id)
        return "这个话题我不知道。"
    return entry.deny_response
