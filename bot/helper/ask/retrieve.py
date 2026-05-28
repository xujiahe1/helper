"""从 bundle + sqlite 向量索引检索相关 spec / entity / raw。

M4 起 hybrid:
  - 路径 A:Jaccard 词重叠(关键词召回,对低频领域名词强)
  - 路径 B:bge-m3 向量召回(语义召回,对同义改写强)
  - 用 RRF (Reciprocal Rank Fusion, k=60) 融合两路 rank,出 top_k

向量召回失败(Athenai 抖动 / embed_index 没配)→ 自动降级只走 Jaccard,不抛错。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import json as _json_top

from sqlalchemy import select

from helper.compiler import load_bundle
from helper.storage import session, vector
from helper.storage.models import (
    CaseCandidate,
    EntityCandidate,
    FactCandidate,
    L1Item,
    L1Result,
    RawInput,
    RelationCandidate,
    SpecCandidate,
)

log = logging.getLogger(__name__)

RRF_K = 60  # RRF 论文默认值,大于命中数即可
JACCARD_TOP_K = 30
VECTOR_TOP_K = 30


@dataclass
class Hit:
    type: str  # spec / entity / raw
    ref: str   # slug 或 raw_id
    title: str
    body: str
    score: float
    sources: list[str] = field(default_factory=list)  # ["jaccard", "vector"] — debug 用


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1}


def _jaccard_score(query_toks: set[str], doc_text: str) -> float:
    doc_toks = _tokens(doc_text)
    if not doc_toks:
        return 0.0
    overlap = query_toks & doc_toks
    return len(overlap) / max(len(query_toks), 1)


# ---------- superseded raw 过滤 ----------


def _superseded_raw_ids() -> set[int]:
    """扫 5 张候选表,凡是 superseded_at 非空的候选,把它支撑的 raw_id 全部收上来。
    这些 raw 在 retrieve 阶段从 raw 命中里剔除 — 否则旧值会跟新值一起被 ask 看到。

    注意:同一条 raw 可能同时支撑多个候选(decision + fact 等),只要其中一个被
    superseded,这条 raw 在该 fact 上下文里就不应再被 ask 引用 — 简化处理:整条 raw
    踢出 retrieve raw 召回。代价是少量误伤(decision 那部分也读不到了),
    但 demo 阶段够用,且 spec/fact 候选 bundle 里通常已有正确版。
    """
    out: set[int] = set()
    with session() as s:
        for cls in (
            SpecCandidate, FactCandidate, CaseCandidate,
            EntityCandidate, RelationCandidate,
        ):
            rows = s.execute(
                select(cls.raw_refs_json).where(cls.superseded_at.is_not(None))
            ).scalars().all()
            for j in rows:
                try:
                    refs = _json_top.loads(j or "[]")
                except _json_top.JSONDecodeError:
                    continue
                for r in refs:
                    if isinstance(r, list) and r and isinstance(r[0], int):
                        out.add(r[0])
                    elif isinstance(r, int):
                        out.add(r)
                    elif isinstance(r, str) and r.isdigit():
                        out.add(int(r))
    return out


# ---------- Jaccard 路径 ----------

def _jaccard_pass(question: str, qtoks: set[str], skip_raw_ids: set[int]) -> list[Hit]:
    """从 bundle 找 spec/entity/fact/case 命中,从 l1_items + raw 找 raw 命中。"""
    import json as _json

    bundle = load_bundle()
    hits: list[Hit] = []

    for spec in bundle.get("specs", []):
        text = " ".join([str(spec.get("title", "")), str(spec.get("_body", ""))])
        sc = _jaccard_score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="spec",
                ref=str(spec.get("slug", "")),
                title=str(spec.get("title", "")),
                body=str(spec.get("_body", ""))[:1000],
                score=sc,
                sources=["jaccard"],
            ))

    for ent in bundle.get("entities", []):
        text = " ".join([str(ent.get("name", "")), str(ent.get("_body", ""))])
        sc = _jaccard_score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="entity",
                ref=str(ent.get("slug", "")),
                title=str(ent.get("name", "")),
                body=str(ent.get("_body", ""))[:600],
                score=sc,
                sources=["jaccard"],
            ))

    for fact in bundle.get("facts", []):
        text = " ".join([str(fact.get("subject", "")), str(fact.get("_body", ""))])
        sc = _jaccard_score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="fact",
                ref=str(fact.get("slug", "")),
                title=str(fact.get("subject", "")) or str(fact.get("slug", "")),
                body=str(fact.get("_body", ""))[:600],
                score=sc,
                sources=["jaccard"],
            ))

    for case in bundle.get("cases", []):
        text = " ".join([str(case.get("title", "")), str(case.get("_body", ""))])
        sc = _jaccard_score(qtoks, text)
        if sc > 0:
            hits.append(Hit(
                type="case",
                ref=str(case.get("slug", "")),
                title=str(case.get("title", "")) or str(case.get("slug", "")),
                body=str(case.get("_body", ""))[:600],
                score=sc,
                sources=["jaccard"],
            ))

    # raw 命中: content_text + 它的所有 L1Item payload 拼成检索文本
    with session() as s:
        ok_raw_ids = [
            r for r in s.execute(
                select(L1Result.raw_id).where(L1Result.error == "")
            ).scalars().all()
            if r not in skip_raw_ids
        ]
        if ok_raw_ids:
            items_by_raw: dict[int, list[str]] = {}
            for it in s.execute(
                select(L1Item).where(L1Item.raw_id.in_(ok_raw_ids))
            ).scalars():
                items_by_raw.setdefault(it.raw_id, []).append(it.payload_json or "")
            for rid in ok_raw_ids:
                raw = s.get(RawInput, rid)
                if raw is None:
                    continue
                payload_text = " ".join(items_by_raw.get(rid, []))
                text = (raw.content_text or "") + " " + payload_text
                sc = _jaccard_score(qtoks, text)
                if sc > 0:
                    title = (raw.content_text or "").replace("\n", " ")[:80] or f"raw#{rid}"
                    hits.append(Hit(
                        type="raw",
                        ref=str(rid),
                        title=title,
                        body=(raw.content_text or "")[:600],
                        score=sc,
                        sources=["jaccard"],
                    ))

    hits.sort(key=lambda h: -h.score)
    return hits[:JACCARD_TOP_K]


# ---------- 向量路径 ----------

def _hydrate_vector_hits(vec_hits: list[vector.VectorHit]) -> list[Hit]:
    """vec0 KNN 给的是 (kind, ref, distance);需要把对应 spec / entity / raw 的标题 + 正文取出来。

    spec / entity 走 bundle(已编译好,正文可读);raw 走 sqlite 直查。
    """
    if not vec_hits:
        return []

    bundle = load_bundle()
    spec_by_slug = {str(sp.get("slug", "")): sp for sp in bundle.get("specs", [])}
    ent_by_slug = {str(ec.get("slug", "")): ec for ec in bundle.get("entities", [])}

    out: list[Hit] = []
    raw_refs_to_fetch = [int(h.ref) for h in vec_hits if h.kind == "raw" and h.ref.isdigit()]
    raw_map: dict[int, RawInput] = {}
    if raw_refs_to_fetch:
        with session() as s:
            for r in s.execute(
                select(RawInput).where(RawInput.id.in_(raw_refs_to_fetch))
            ).scalars():
                raw_map[r.id] = r

    # vector 路径距离越小越相关。融合用的是 rank,score 这里只为可读性留 1/(1+dist)
    for h in vec_hits:
        readable = 1.0 / (1.0 + h.distance)
        if h.kind == "spec":
            sp = spec_by_slug.get(h.ref)
            if sp is None:
                continue
            out.append(Hit(
                type="spec",
                ref=h.ref,
                title=str(sp.get("title", "")),
                body=str(sp.get("_body", ""))[:1000],
                score=readable,
                sources=["vector"],
            ))
        elif h.kind == "entity":
            ec = ent_by_slug.get(h.ref)
            if ec is None:
                continue
            out.append(Hit(
                type="entity",
                ref=h.ref,
                title=str(ec.get("name", "")),
                body=str(ec.get("_body", ""))[:600],
                score=readable,
                sources=["vector"],
            ))
        elif h.kind == "raw":
            try:
                rid = int(h.ref)
            except ValueError:
                continue
            r = raw_map.get(rid)
            if r is None:
                continue
            out.append(Hit(
                type="raw",
                ref=h.ref,
                title=(r.content_text or "")[:80],
                body=(r.content_text or "")[:600],
                score=readable,
                sources=["vector"],
            ))
    return out


def _vector_pass(question: str, skip_raw_ids: set[int]) -> list[Hit]:
    try:
        with session() as s:
            vec_hits = vector.search(s, query=question, top_k=VECTOR_TOP_K)
    except Exception as e:  # noqa: BLE001
        log.warning("vector_pass failed err=%s", e)
        return []
    hydrated = _hydrate_vector_hits(vec_hits)
    if not skip_raw_ids:
        return hydrated
    out = []
    for h in hydrated:
        if h.type == "raw" and h.ref.isdigit() and int(h.ref) in skip_raw_ids:
            continue
        out.append(h)
    return out


# ---------- RRF 融合 ----------

def _rrf_fuse(jaccard: list[Hit], vec: list[Hit], top_k: int) -> list[Hit]:
    """对两路 rank list 做 Reciprocal Rank Fusion。

    rrf_score(item) = Σ over lists [ 1 / (k + rank_in_that_list) ]
    rank 从 1 开始;不在某个 list 里就不贡献。
    """
    fused: dict[tuple[str, str], Hit] = {}
    score: dict[tuple[str, str], float] = {}

    for rank, h in enumerate(jaccard, start=1):
        key = (h.type, h.ref)
        score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
        if key not in fused:
            fused[key] = Hit(
                type=h.type, ref=h.ref, title=h.title, body=h.body,
                score=0.0, sources=list(h.sources),
            )
        elif "jaccard" not in fused[key].sources:
            fused[key].sources.append("jaccard")

    for rank, h in enumerate(vec, start=1):
        key = (h.type, h.ref)
        score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
        if key not in fused:
            fused[key] = Hit(
                type=h.type, ref=h.ref, title=h.title, body=h.body,
                score=0.0, sources=list(h.sources),
            )
        elif "vector" not in fused[key].sources:
            fused[key].sources.append("vector")

    # spec 维持"权重高"语义,fact/case 与 entity 同档,raw 略低
    for key, h in fused.items():
        s = score[key]
        if h.type == "spec":
            s *= 1.5
        elif h.type == "raw":
            s *= 0.8
        h.score = s

    out = sorted(fused.values(), key=lambda x: -x.score)
    return out[:top_k]


# ---------- 入口 ----------

def retrieve_relevant(question: str, *, top_k: int = 8) -> list[Hit]:
    """对 question 做检索,返 top_k Hit。

    Hybrid:Jaccard + 向量,RRF 融合;向量失败自动降级只走 Jaccard。
    """
    qtoks = _tokens(question)
    if not qtoks:
        return []

    skip = _superseded_raw_ids()
    jac = _jaccard_pass(question, qtoks, skip)
    vec = _vector_pass(question, skip)

    if not vec:
        # 向量没出活:走纯 Jaccard 旧逻辑(spec 权重 * 1.5, raw 权重 * 0.6)
        for h in jac:
            if h.type == "spec":
                h.score *= 1.5
            elif h.type == "raw":
                h.score *= 0.6
        return jac[:top_k]

    return _rrf_fuse(jac, vec, top_k)
