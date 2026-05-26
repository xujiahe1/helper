"""从 bundle 检索相关 spec / entity / raw。

M1: 关键词命中 + entity 共现,无 embedding。M3 上 sqlite-vec 再加。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select

from helper.compiler import load_bundle
from helper.storage import session
from helper.storage.models import L1Result, RawInput


@dataclass
class Hit:
    type: str  # spec / entity / raw
    ref: str   # slug 或 raw_id
    title: str
    body: str
    score: float


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1}


def _score(query_toks: set[str], doc_text: str) -> float:
    doc_toks = _tokens(doc_text)
    if not doc_toks:
        return 0.0
    overlap = query_toks & doc_toks
    return len(overlap) / max(len(query_toks), 1)


def retrieve_relevant(question: str, *, top_k: int = 8) -> list[Hit]:
    """对 question 做检索,返 top_k Hit。"""
    bundle = load_bundle()
    qtoks = _tokens(question)
    if not qtoks:
        return []

    hits: list[Hit] = []

    for spec in bundle.get("specs", []):
        text = " ".join([
            str(spec.get("title", "")),
            str(spec.get("_body", "")),
        ])
        sc = _score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="spec",
                ref=str(spec.get("slug", "")),
                title=str(spec.get("title", "")),
                body=str(spec.get("_body", ""))[:1000],
                score=sc * 1.5,  # spec 权重高
            ))

    for ent in bundle.get("entities", []):
        text = " ".join([
            str(ent.get("name", "")),
            str(ent.get("_body", "")),
        ])
        sc = _score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="entity",
                ref=str(ent.get("slug", "")),
                title=str(ent.get("name", "")),
                body=str(ent.get("_body", ""))[:600],
                score=sc * 1.0,
            ))

    # raw 兜底:bundle 没命中也从 sqlite 直接捞
    if not hits:
        with session() as s:
            l1s = s.execute(select(L1Result).where(L1Result.error == "")).scalars().all()
            for l1 in l1s:
                text = " ".join([l1.scene, l1.choice, l1.rationale, l1.signals_json])
                sc = _score(qtoks, text)
                if sc > 0:
                    raw = s.get(RawInput, l1.raw_id)
                    hits.append(Hit(
                        type="raw",
                        ref=str(l1.raw_id),
                        title=l1.scene[:80] if l1.scene else f"raw#{l1.raw_id}",
                        body=(raw.content_text if raw else "")[:600],
                        score=sc * 0.6,
                    ))

    hits.sort(key=lambda h: -h.score)
    return hits[:top_k]
