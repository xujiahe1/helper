"""Entity 晋升 — 从 sqlite 候选到 git ontology/entities/<slug>.md。

策略走 meta/policies/knowledge_policy.yaml 的 entity_promotion。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from sqlalchemy import select

from helper.config import get_settings
from helper.policy import load_knowledge_policy
from helper.policy.knowledge import should_promote
from helper.storage import session
from helper.storage.models import EntityCandidate

log = logging.getLogger(__name__)

ENTITIES_RELDIR = Path("ontology") / "entities"


def _frontmatter(ec: EntityCandidate) -> str:
    refs = json.loads(ec.raw_refs_json or "[]")
    fm = [
        "---",
        f"slug: {ec.slug}",
        f"name: {ec.name}",
        f"entity_type: {ec.entity_type}",
        f"first_seen: {ec.first_seen.isoformat() if ec.first_seen else ''}",
        f"last_seen: {ec.last_seen.isoformat() if ec.last_seen else ''}",
        f"mention_count: {ec.mention_count}",
        f"raw_refs: {refs}",
        "---",
        "",
    ]
    return "\n".join(fm)


def _md_body(ec: EntityCandidate) -> str:
    parts = [f"# {ec.name}\n"]
    if ec.description:
        parts.append(ec.description + "\n")
    parts.append("\n## Raw 来源\n")
    refs = json.loads(ec.raw_refs_json or "[]")
    for r in refs:
        parts.append(f"- raw#{r}")
    return "\n".join(parts) + "\n"


def promote_one(slug: str) -> str | None:
    """把指定 slug 晋升到 git。已晋升过的 = 刷新内容(增量 raw 引用)。

    返回 git 相对路径,若不应晋升 / 找不到则 None。
    """
    s = get_settings()
    policy = load_knowledge_policy(s.helper_spec_git_dir)

    with session() as sess:
        ec = sess.execute(
            select(EntityCandidate).where(EntityCandidate.slug == slug)
        ).scalar_one_or_none()
        if ec is None:
            return None
        refs = json.loads(ec.raw_refs_json or "[]")
        # 是否达晋升阈值。M1 没建 spec relation,先放宽 require_spec_relation。
        if not should_promote(
            policy,
            ec.entity_type,
            raw_ref_count=len(refs),
            has_spec_relation=True,  # M1: 暂用 True 让阈值只看 mention_count
        ):
            return None

        rel = ENTITIES_RELDIR / f"{ec.slug}.md"
        abs_path = s.helper_spec_git_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(_frontmatter(ec) + _md_body(ec), encoding="utf-8")

        if ec.promoted_at is None:
            ec.promoted_at = datetime.now(timezone.utc)
        ec.git_path = str(rel)
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        repo.index.commit(f"ontology: promote {slug}")
    return str(rel)


def promote_eligible(*, limit: int = 50) -> list[str]:
    """扫所有候选,把够格的全部晋升。返回晋升的 slug 列表。"""
    s = get_settings()
    policy = load_knowledge_policy(s.helper_spec_git_dir)

    with session() as sess:
        cands = sess.execute(
            select(EntityCandidate)
            .where(EntityCandidate.promoted_at.is_(None))
            .order_by(EntityCandidate.mention_count.desc())
            .limit(limit)
        ).scalars().all()
        eligible: list[str] = []
        for ec in cands:
            refs = json.loads(ec.raw_refs_json or "[]")
            if should_promote(policy, ec.entity_type, len(refs), has_spec_relation=True):
                eligible.append(ec.slug)

    promoted: list[str] = []
    for slug in eligible:
        path = promote_one(slug)
        if path:
            promoted.append(slug)
    return promoted
