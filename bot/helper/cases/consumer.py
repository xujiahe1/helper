"""把 L1Item.type=case 的原子收口到 case_candidates。

case 是 episode 级 — 每个 case 通常独立成行。slug 由 scene+what_happened 派,
同一案例被复述时 mention_count++。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import CaseCandidate, L1Item

log = logging.getLogger(__name__)


_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slugify_case(scene: str, what_happened: str) -> str:
    base = " ".join([scene, what_happened])[:200]
    s = unicodedata.normalize("NFKC", base).strip().lower()
    s = _NON_WORD_RE.sub("_", s).strip("_")[:96]
    h = hashlib.md5((scene + "|" + what_happened).encode("utf-8")).hexdigest()[:8]
    return f"{s}_{h}" if s else f"case_{h}"


def consume_case_items(raw_id: int) -> list[CaseCandidate]:
    now = datetime.now(timezone.utc)
    out: list[CaseCandidate] = []
    with session() as s:
        items = s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id, L1Item.type == "case")
        ).scalars().all()
        if not items:
            return []

        for it in items:
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            scene = str(payload.get("scene", "")).strip()
            what_happened = str(payload.get("what_happened", "")).strip()
            outcome = str(payload.get("outcome", "")).strip()
            referenced_spec = str(payload.get("referenced_spec", "")).strip()[:128]
            if not (scene or what_happened):
                continue
            slug = _slugify_case(scene, what_happened)
            title = (scene[:80] or what_happened[:80] or "case").strip()
            ref = [raw_id, it.idx]

            existing = s.execute(
                select(CaseCandidate).where(CaseCandidate.slug == slug)
            ).scalar_one_or_none()
            if existing is None:
                row = CaseCandidate(
                    slug=slug,
                    title=title,
                    scene=scene,
                    what_happened=what_happened,
                    outcome=outcome,
                    referenced_spec=referenced_spec,
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
                if not existing.outcome and outcome:
                    existing.outcome = outcome
                if not existing.referenced_spec and referenced_spec:
                    existing.referenced_spec = referenced_spec
                out.append(existing)
        s.commit()
        return [s.get(CaseCandidate, e.id) for e in out if e.id is not None]
