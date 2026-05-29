"""Relations — 实体间关系候选 + 晋升。

L1Item.type=relation → relation_candidates(sqlite),阈值达标晋升到 git
ontology/relationships/<slug>.md。

阈值: mention_count >= 2(相比 fact 更需要佐证 — 关系判断更容易出错)。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from sqlalchemy import select

from helper.config import get_settings
from helper.storage import session
from helper.storage.models import L1Item, RelationCandidate

log = logging.getLogger(__name__)

RELATIONS_RELDIR = Path("ontology") / "relationships"
MIN_MENTION_TO_PROMOTE = 2

_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    return _NON_WORD_RE.sub("_", s).strip("_")[:64]


def _slugify(a: str, rel: str, b: str) -> str:
    parts = [_normalize(a), _normalize(rel), _normalize(b)]
    if not all(parts):
        return ""
    return "__".join(parts)[:255]


def consume_relation_items(raw_id: int) -> list[RelationCandidate]:
    """收口 raw_id 对应的所有 L1Item.type=relation。"""
    now = datetime.now(timezone.utc)
    out: list[RelationCandidate] = []
    with session() as s:
        items = s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id, L1Item.type == "relation")
        ).scalars().all()
        if not items:
            return []

        for it in items:
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            entity_a = str(payload.get("entity_a", "")).strip()
            relation = str(payload.get("relation", "")).strip()
            entity_b = str(payload.get("entity_b", "")).strip()
            description = str(payload.get("description", "")).strip()
            if not (entity_a and relation and entity_b):
                continue
            slug = _slugify(entity_a, relation, entity_b)
            if not slug:
                continue
            ref = [raw_id, it.idx]

            existing = s.execute(
                select(RelationCandidate).where(RelationCandidate.slug == slug)
            ).scalar_one_or_none()
            if existing is None:
                row = RelationCandidate(
                    slug=slug,
                    entity_a=entity_a[:128],
                    relation=_normalize(relation) or relation[:64],
                    entity_b=entity_b[:128],
                    description=description,
                    raw_refs_json=json.dumps([ref]),
                    mention_count=1,
                    first_seen=now,
                    last_seen=now,
                )
                s.add(row)
                out.append(row)
            else:
                refs = json.loads(existing.raw_refs_json or "[]")
                if ref not in refs:
                    refs.append(ref)
                    existing.mention_count += 1
                existing.raw_refs_json = json.dumps(refs)
                existing.last_seen = now
                if not existing.description and description:
                    existing.description = description
                out.append(existing)
        s.commit()
        for row in out:
            try:
                from helper.storage import fts, vector as _vec
                fts.index_relation(s, row.slug)
                _vec.index_relation(s, row.slug)
            except Exception:  # noqa: BLE001
                log.exception("index relation failed slug=%s", row.slug)
        s.commit()
        return [s.get(RelationCandidate, e.id) for e in out if e.id is not None]


def _md(rc: RelationCandidate) -> str:
    refs = json.loads(rc.raw_refs_json or "[]")
    fm = [
        "---",
        f"slug: {rc.slug}",
        f"entity_a: {rc.entity_a}",
        f"relation: {rc.relation}",
        f"entity_b: {rc.entity_b}",
        f"first_seen: {rc.first_seen.isoformat() if rc.first_seen else ''}",
        f"last_seen: {rc.last_seen.isoformat() if rc.last_seen else ''}",
        f"mention_count: {rc.mention_count}",
        f"raw_refs: {refs}",
        "---",
        "",
        f"# {rc.entity_a} —[{rc.relation}]→ {rc.entity_b}",
        "",
    ]
    if rc.description:
        fm.extend([rc.description, ""])
    fm.extend(["## Raw 来源", ""])
    for r in refs:
        fm.append(f"- raw#{r[0]}#{r[1]}" if isinstance(r, list) and len(r) == 2 else f"- raw#{r}")
    return "\n".join(fm) + "\n"


def promote_one(slug: str) -> str | None:
    s = get_settings()
    with session() as sess:
        rc = sess.execute(
            select(RelationCandidate).where(RelationCandidate.slug == slug)
        ).scalar_one_or_none()
        if rc is None or rc.mention_count < MIN_MENTION_TO_PROMOTE:
            return None
        rel = RELATIONS_RELDIR / f"{rc.slug}.md"
        abs_path = s.helper_spec_git_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(_md(rc), encoding="utf-8")
        if rc.promoted_at is None:
            rc.promoted_at = datetime.now(timezone.utc)
        rc.git_path = str(rel)
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        repo.index.commit(f"relations: promote {slug}")
    return str(rel)


def promote_eligible(*, limit: int = 100) -> list[str]:
    with session() as sess:
        cands = sess.execute(
            select(RelationCandidate)
            .where(RelationCandidate.promoted_at.is_(None))
            .where(RelationCandidate.mention_count >= MIN_MENTION_TO_PROMOTE)
            .order_by(RelationCandidate.mention_count.desc())
            .limit(limit)
        ).scalars().all()
        slugs = [rc.slug for rc in cands]
    return [s for s in slugs if promote_one(s) is not None]
