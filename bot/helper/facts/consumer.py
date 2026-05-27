"""把 L1Item.type=fact 的原子收口到 fact_candidates。

slug 派生策略: f"{subject}_{predicate}_{object}" 折叠成 slug,保证同一陈述
在多次抽取里收敛到同一行(mention_count++)。
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
from helper.storage.models import FactCandidate, L1Item

log = logging.getLogger(__name__)


_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slugify_fact(subject: str, predicate: str, obj: str) -> str:
    """主谓宾 → slug。中文按字符保留;太长用 hash 截尾保证全局唯一。"""
    parts = [subject, predicate, obj]
    base = "_".join(p for p in parts if p)
    s = unicodedata.normalize("NFKC", base).strip().lower()
    s = _NON_WORD_RE.sub("_", s).strip("_")
    if len(s) <= 96:
        return s
    h = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    return f"{s[:96]}_{h}"


def consume_fact_items(raw_id: int) -> list[FactCandidate]:
    """收口 raw_id 对应的所有 L1Item.type=fact。"""
    now = datetime.now(timezone.utc)
    out: list[FactCandidate] = []
    with session() as s:
        items = s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id, L1Item.type == "fact")
        ).scalars().all()
        if not items:
            return []

        for it in items:
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            subject = str(payload.get("subject", "")).strip()
            predicate = str(payload.get("predicate", "")).strip()
            obj = str(payload.get("object", "")).strip()
            scope = str(payload.get("scope", "")).strip()
            if not (subject and predicate):
                continue
            slug = _slugify_fact(subject, predicate, obj)
            if not slug:
                continue
            statement = f"{subject} {predicate} {obj}".strip()
            ref = [raw_id, it.idx]

            existing = s.execute(
                select(FactCandidate).where(FactCandidate.slug == slug)
            ).scalar_one_or_none()
            if existing is None:
                row = FactCandidate(
                    slug=slug,
                    statement=statement[:2000],
                    subject=subject[:255],
                    predicate=predicate[:255],
                    object=obj,
                    scope=scope,
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
                if not existing.scope and scope:
                    existing.scope = scope
                out.append(existing)
        s.commit()
        return [s.get(FactCandidate, e.id) for e in out if e.id is not None]
