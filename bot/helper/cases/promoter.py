"""Case 晋升 — sqlite 候选 → git cases/<slug>.md。

阈值: mention_count >= 1。case 是 episode 级,1 次提及就值得记。
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
from helper.storage.models import CaseCandidate

log = logging.getLogger(__name__)

CASES_RELDIR = Path("cases")
MIN_MENTION_TO_PROMOTE = 1


def _md(cc: CaseCandidate) -> str:
    refs = json.loads(cc.raw_refs_json or "[]")
    fm = [
        "---",
        f"slug: {cc.slug}",
        f"title: {cc.title}",
        f"referenced_spec: {cc.referenced_spec}",
        f"first_seen: {cc.first_seen.isoformat() if cc.first_seen else ''}",
        f"last_seen: {cc.last_seen.isoformat() if cc.last_seen else ''}",
        f"mention_count: {cc.mention_count}",
        f"raw_refs: {refs}",
        "---",
        "",
        f"# {cc.title}",
        "",
    ]
    if cc.scene:
        fm.extend(["## 场景", "", cc.scene, ""])
    if cc.what_happened:
        fm.extend(["## 经过", "", cc.what_happened, ""])
    if cc.outcome:
        fm.extend(["## 结果", "", cc.outcome, ""])
    if cc.referenced_spec:
        fm.extend(["## 关联规约", "", f"- {cc.referenced_spec}", ""])
    fm.extend(["## Raw 来源", ""])
    for r in refs:
        fm.append(f"- raw#{r[0]}#{r[1]}" if isinstance(r, list) and len(r) == 2 else f"- raw#{r}")
    return "\n".join(fm) + "\n"


def promote_one(slug: str) -> str | None:
    s = get_settings()
    with session() as sess:
        cc = sess.execute(
            select(CaseCandidate).where(CaseCandidate.slug == slug)
        ).scalar_one_or_none()
        if cc is None or cc.mention_count < MIN_MENTION_TO_PROMOTE:
            return None
        rel = CASES_RELDIR / f"{cc.slug}.md"
        abs_path = s.helper_spec_git_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(_md(cc), encoding="utf-8")
        if cc.promoted_at is None:
            cc.promoted_at = datetime.now(timezone.utc)
        cc.git_path = str(rel)
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        repo.index.commit(f"cases: promote {slug}")
    return str(rel)


def promote_eligible(*, limit: int = 100) -> list[str]:
    with session() as sess:
        cands = sess.execute(
            select(CaseCandidate)
            .where(CaseCandidate.promoted_at.is_(None))
            .where(CaseCandidate.mention_count >= MIN_MENTION_TO_PROMOTE)
            .order_by(CaseCandidate.mention_count.desc())
            .limit(limit)
        ).scalars().all()
        slugs = [cc.slug for cc in cands]
    return [s for s in slugs if promote_one(s) is not None]
