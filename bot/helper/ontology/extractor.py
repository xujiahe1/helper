"""把 L1Item.type=concept 的原子收口到 entity_candidates。

老的"二次 LLM 抽 entity"链路不再使用 — L1 多类型抽取已经直接产出 concept 原子,
带 {name, entity_type, description}。这里只做去重 / mention 累加 / 失活引用合并。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import EntityCandidate, L1Item

log = logging.getLogger(__name__)


_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slugify(name: str) -> str:
    """name → slug。中文按字符保留,空白 / 标点折叠成下划线。"""
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    s = _NON_WORD_RE.sub("_", s).strip("_")
    return s[:128]


def consume_concept_items(raw_id: int) -> list[EntityCandidate]:
    """把 raw_id 对应的所有 L1Item.type=concept 收口到 entity_candidates。

    每条 concept item 用 {name} 派 slug;同 slug → mention_count++、ref 加进
    raw_refs_json([[raw_id, idx], ...])。返回受影响的 EntityCandidate。
    """
    now = datetime.now(timezone.utc)
    out: list[EntityCandidate] = []
    with session() as s:
        items = s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id, L1Item.type == "concept")
        ).scalars().all()
        if not items:
            return []

        for it in items:
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            name = str(payload.get("name", "")).strip()[:255]
            if not name:
                continue
            slug = _slugify(name)
            if not slug:
                continue
            etype = str(payload.get("entity_type", "")).strip() or "decision_concept"
            desc = str(payload.get("description", "")).strip()
            ref = [raw_id, it.idx]

            existing = s.execute(
                select(EntityCandidate).where(EntityCandidate.slug == slug)
            ).scalar_one_or_none()
            if existing is None:
                row = EntityCandidate(
                    slug=slug,
                    name=name,
                    entity_type=etype,
                    description=desc,
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
                if not existing.description and desc:
                    existing.description = desc
                out.append(existing)
        s.commit()
        for row in out:
            try:
                from helper.storage import fts
                fts.index_entity(s, row.slug)
            except Exception:  # noqa: BLE001
                log.exception("fts.index_entity failed slug=%s", row.slug)
        s.commit()
        return [s.get(EntityCandidate, e.id) for e in out if e.id is not None]
