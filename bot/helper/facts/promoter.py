"""Fact 晋升 — sqlite 候选 → git facts/<slug>.md。

阈值: mention_count >= 1(默认全晋升 — fact 是单条陈述,不需要聚类佐证)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from sqlalchemy import select

from helper.config import get_settings
from helper.storage import session
from helper.storage.models import FactCandidate

log = logging.getLogger(__name__)

FACTS_RELDIR = Path("facts")
MIN_MENTION_TO_PROMOTE = 1


def _md(fc: FactCandidate) -> str:
    refs = json.loads(fc.raw_refs_json or "[]")
    fm = [
        "---",
        f"slug: {fc.slug}",
        f"subject: {fc.subject}",
        f"predicate: {fc.predicate}",
        f"object: {fc.object}",
        f"scope: {fc.scope}",
        f"first_seen: {fc.first_seen.isoformat() if fc.first_seen else ''}",
        f"last_seen: {fc.last_seen.isoformat() if fc.last_seen else ''}",
        f"mention_count: {fc.mention_count}",
        f"raw_refs: {refs}",
        "---",
        "",
        f"# {fc.statement}",
        "",
    ]
    if fc.scope:
        fm.extend(["## 适用范围", "", fc.scope, ""])
    fm.extend(["## Raw 来源", ""])
    for r in refs:
        fm.append(f"- raw#{r[0]}#{r[1]}" if isinstance(r, list) and len(r) == 2 else f"- raw#{r}")
    return "\n".join(fm) + "\n"


def promote_one(slug: str) -> str | None:
    s = get_settings()
    with session() as sess:
        fc = sess.execute(
            select(FactCandidate).where(FactCandidate.slug == slug)
        ).scalar_one_or_none()
        if fc is None or fc.mention_count < MIN_MENTION_TO_PROMOTE:
            return None
        rel = FACTS_RELDIR / f"{fc.slug}.md"
        abs_path = s.helper_spec_git_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(_md(fc), encoding="utf-8")
        if fc.promoted_at is None:
            fc.promoted_at = datetime.now(timezone.utc)
        fc.git_path = str(rel)
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        repo.index.commit(f"facts: promote {slug}")
    return str(rel)


def promote_eligible(*, limit: int = 100) -> list[str]:
    with session() as sess:
        cands = sess.execute(
            select(FactCandidate)
            .where(FactCandidate.promoted_at.is_(None))
            .where(FactCandidate.mention_count >= MIN_MENTION_TO_PROMOTE)
            .order_by(FactCandidate.mention_count.desc())
            .limit(limit)
        ).scalars().all()
        slugs = [fc.slug for fc in cands]
    return [s for s in slugs if promote_one(s) is not None]
