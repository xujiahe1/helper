"""从 bundle + sqlite FTS5 + 向量索引检索相关 spec / entity / raw / fact / case / relation。

M7 起 hybrid:
  - 路径 A:bundle 内存 Jaccard(几十~几百条 spec/entity 摘要,直接全扫便宜)
  - 路径 B:FTS5 + jieba 词面召回(raw / 4 类候选表;替换原 Jaccard 全扫)
  - 路径 C:bge-m3 向量召回(对同义改写强;raw + 已晋升 spec/entity/fact/case/relation)
  - 用 RRF (Reciprocal Rank Fusion, k=60) 融合所有路 rank,出 top_k

向量 / FTS 失败 → 各自路径降级返空,其余路仍正常工作。
为啥 bundle 还走 Jaccard:bundle 是已编译的小集合(内存),走 FTS 反而要先 jieba 切词
再 sqlite 一趟,得不偿失。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import json as _json_top

from sqlalchemy import select

from helper.compiler import load_bundle
from helper.storage import fts, session, vector
from helper.storage.models import (
    CaseCandidate,
    EntityCandidate,
    FactCandidate,
    RawInput,
    RelationCandidate,
    SpecCandidate,
)

log = logging.getLogger(__name__)

RRF_K = 60  # RRF 论文默认值,大于命中数即可
JACCARD_TOP_K = 30
VECTOR_TOP_K = 30
FTS_TOP_K = 30


@dataclass
class Hit:
    type: str  # spec / entity / raw
    ref: str   # slug 或 raw_id
    title: str
    body: str
    score: float
    sources: list[str] = field(default_factory=list)  # ["jaccard", "vector"] — debug 用


# 中文 / 英文混合分词:
# - 英文/数字: 按非字母数字边界切单词(原行为保留,大于 1 字符才进集合)
# - 中文: 字符 2-gram(连续 CJK 字符串切成相邻字对),让"账号关联""关联账号"
#   能匹配"账号关联绑定"
# 旧实现 [\w一-鿿]+ 把整段中文当一个 token,导致中文 query 在中文 doc 里几乎
# 永远 Jaccard=0。
_CJK_RE = re.compile(r"[㐀-鿿]+")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _cjk_bigrams(s: str) -> list[str]:
    """连续 CJK 串切 2-gram。长度 1 直接当 token。"""
    if len(s) <= 1:
        return [s]
    return [s[i : i + 2] for i in range(len(s) - 1)]


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    out: set[str] = set()
    # ASCII 单词:小写化,长度 ≥2
    for m in _ASCII_TOKEN_RE.findall(text):
        if len(m) >= 2:
            out.add(m.lower())
    # CJK 串:2-gram
    for m in _CJK_RE.findall(text):
        for bg in _cjk_bigrams(m):
            if bg.strip():
                out.add(bg)
    return out


def _jaccard_score(query_toks: set[str], doc_text: str) -> float:
    doc_toks = _tokens(doc_text)
    if not doc_toks:
        return 0.0
    overlap = query_toks & doc_toks
    if not overlap:
        return 0.0
    # 用 query 长度做分母:doc 长不该把 score 拉低(Jaccard 标准定义是
    # |A∩B|/|A∪B|,但这里 doc 通常远长于 query,标准 Jaccard 会让短 query
    # 在长 doc 里得分被稀释。换成 overlap/|query| 更贴召回直觉)
    return len(overlap) / max(len(query_toks), 1)


# ---------- superseded raw 过滤(差集策略) ----------

# 5 张候选表 + 各自的 raw_refs 字段名(SpecCandidate 历史叫 cluster_raw_ids_json)
_CANDIDATE_TABLES: list[tuple[type, str]] = [
    (SpecCandidate, "cluster_raw_ids_json"),
    (FactCandidate, "raw_refs_json"),
    (CaseCandidate, "raw_refs_json"),
    (EntityCandidate, "raw_refs_json"),
    (RelationCandidate, "raw_refs_json"),
]


def _parse_raw_refs(j: str | None) -> set[int]:
    """raw_refs_json 兼容三种格式:[[raw_id, idx], ...] / [raw_id, ...] / ["raw_id", ...]。
    解析失败返空集。"""
    try:
        refs = _json_top.loads(j or "[]")
    except _json_top.JSONDecodeError:
        return set()
    out: set[int] = set()
    for r in refs:
        if isinstance(r, list) and r and isinstance(r[0], int):
            out.add(r[0])
        elif isinstance(r, int):
            out.add(r)
        elif isinstance(r, str) and r.isdigit():
            out.add(int(r))
    return out


def _superseded_raw_ids() -> set[int]:
    """返回"应该被 retrieve 过滤掉的 raw_id"集合。

    策略:差集 = (任一已 superseded 候选支撑的 raw) − (任一仍 alive 候选支撑的 raw)。
    一条 raw 只要还撑着任何一个未 superseded 候选(decision/fact/concept 等),
    它在 retrieve 里仍可被召回 — 只过滤"仅被 superseded 候选独占引用"的 raw。

    这样消除以下误伤:同一条 raw 抽出 decision + fact,其中 fact 后来被 supersede,
    decision 仍然有效 → raw 应保留。
    """
    superseded: set[int] = set()
    alive: set[int] = set()
    with session() as s:
        for cls, field_name in _CANDIDATE_TABLES:
            col = getattr(cls, field_name)
            sup_rows = s.execute(
                select(col).where(cls.superseded_at.is_not(None))
            ).scalars().all()
            for j in sup_rows:
                superseded |= _parse_raw_refs(j)
            alive_rows = s.execute(
                select(col).where(cls.superseded_at.is_(None))
            ).scalars().all()
            for j in alive_rows:
                alive |= _parse_raw_refs(j)
    return superseded - alive


# ---------- FTS 路径(raw + 5 类候选,替代旧 Jaccard 全扫) ----------


def _hydrate_fts_hits(
    fts_hits: list[tuple[str, str, float]],
    skip_raw_ids: set[int],
) -> list[Hit]:
    """fts.search 给的 (kind, ref, score) 反查正文,组成 Hit。

    raw   → RawInput.content_text(过 skip)
    spec  → 优先 bundle(已 review),没有再回 SpecCandidate
    entity/fact/case/relation → 候选表(过滤 superseded_at)
    bundle 命中的 spec / entity 这里也会被反查到,但 _bundle_jaccard_pass 也会出
    同 (type, ref),由 RRF 融合层去重。
    """
    if not fts_hits:
        return []

    # 按 kind 拆 ref 列表
    by_kind: dict[str, list[str]] = {}
    score_map: dict[tuple[str, str], float] = {}
    for kind, ref, score in fts_hits:
        if kind == "raw":
            try:
                if int(ref) in skip_raw_ids:
                    continue
            except ValueError:
                continue
        by_kind.setdefault(kind, []).append(ref)
        score_map[(kind, ref)] = score

    out: list[Hit] = []
    with session() as s:
        if "raw" in by_kind:
            ids = [int(r) for r in by_kind["raw"] if r.isdigit()]
            if ids:
                rows = {
                    r.id: r for r in s.execute(
                        select(RawInput).where(RawInput.id.in_(ids))
                    ).scalars()
                }
                for ref in by_kind["raw"]:
                    rid = int(ref) if ref.isdigit() else None
                    if rid is None or rid not in rows:
                        continue
                    r = rows[rid]
                    title = (r.content_text or "").replace("\n", " ")[:80] or f"raw#{rid}"
                    out.append(Hit(
                        type="raw", ref=ref, title=title,
                        body=(r.content_text or "")[:600],
                        score=score_map[("raw", ref)], sources=["fts"],
                    ))
        if "spec" in by_kind:
            slugs = by_kind["spec"]
            rows = {
                sc.slug: sc for sc in s.execute(
                    select(SpecCandidate)
                    .where(SpecCandidate.slug.in_(slugs))
                    .where(SpecCandidate.superseded_at.is_(None))
                ).scalars()
            }
            for slug in slugs:
                sc = rows.get(slug)
                if sc is None:
                    continue
                out.append(Hit(
                    type="spec", ref=slug, title=sc.title or slug,
                    body=(sc.statement or "") + ("\n" + sc.rationale if sc.rationale else ""),
                    score=score_map[("spec", slug)], sources=["fts"],
                ))
        if "entity" in by_kind:
            slugs = by_kind["entity"]
            rows = {
                ec.slug: ec for ec in s.execute(
                    select(EntityCandidate)
                    .where(EntityCandidate.slug.in_(slugs))
                    .where(EntityCandidate.superseded_at.is_(None))
                ).scalars()
            }
            for slug in slugs:
                ec = rows.get(slug)
                if ec is None:
                    continue
                out.append(Hit(
                    type="entity", ref=slug, title=ec.name or slug,
                    body=ec.description or "",
                    score=score_map[("entity", slug)], sources=["fts"],
                ))
        if "fact" in by_kind:
            slugs = by_kind["fact"]
            rows = {
                fc.slug: fc for fc in s.execute(
                    select(FactCandidate)
                    .where(FactCandidate.slug.in_(slugs))
                    .where(FactCandidate.superseded_at.is_(None))
                ).scalars()
            }
            for slug in slugs:
                fc = rows.get(slug)
                if fc is None:
                    continue
                out.append(Hit(
                    type="fact", ref=slug,
                    title=f"{fc.subject} {fc.predicate} {fc.object}".strip(),
                    body=fc.statement or "",
                    score=score_map[("fact", slug)], sources=["fts"],
                ))
        if "case" in by_kind:
            slugs = by_kind["case"]
            rows = {
                cc.slug: cc for cc in s.execute(
                    select(CaseCandidate)
                    .where(CaseCandidate.slug.in_(slugs))
                    .where(CaseCandidate.superseded_at.is_(None))
                ).scalars()
            }
            for slug in slugs:
                cc = rows.get(slug)
                if cc is None:
                    continue
                out.append(Hit(
                    type="case", ref=slug, title=cc.title or slug,
                    body=(cc.what_happened or "") + " | " + (cc.outcome or ""),
                    score=score_map[("case", slug)], sources=["fts"],
                ))
        if "relation" in by_kind:
            slugs = by_kind["relation"]
            rows = {
                rc.slug: rc for rc in s.execute(
                    select(RelationCandidate)
                    .where(RelationCandidate.slug.in_(slugs))
                    .where(RelationCandidate.superseded_at.is_(None))
                ).scalars()
            }
            for slug in slugs:
                rc = rows.get(slug)
                if rc is None:
                    continue
                out.append(Hit(
                    type="relation", ref=slug,
                    title=f"{rc.entity_a} —[{rc.relation}]→ {rc.entity_b}",
                    body=rc.description or "",
                    score=score_map[("relation", slug)], sources=["fts"],
                ))
    return out


def _fts_pass(question: str, skip_raw_ids: set[int]) -> list[Hit]:
    """FTS5 + jieba 词面召回,覆盖 raw + 5 类候选。"""
    try:
        with session() as s:
            hits = fts.search(s, query=question, top_k=FTS_TOP_K)
    except Exception as e:  # noqa: BLE001
        log.warning("fts_pass failed err=%s", e)
        return []
    return _hydrate_fts_hits(hits, skip_raw_ids)


# ---------- bundle 内存 Jaccard 路径(只扫已编译的小集合) ----------

def _bundle_jaccard_pass(qtoks: set[str]) -> list[Hit]:
    """bundle 是已编译的 spec/entity/fact/case 摘要(几十~几百条),内存全扫便宜。

    raw / 候选表的全扫由 _fts_pass 走 FTS5(几万行规模秒级 → 毫秒级)。
    """
    bundle = load_bundle()
    hits: list[Hit] = []

    for spec in bundle.get("specs", []):
        text_ = " ".join([str(spec.get("title", "")), str(spec.get("_body", ""))])
        sc = _jaccard_score(qtoks, text_)
        if sc > 0:
            hits.append(Hit(
                type="spec", ref=str(spec.get("slug", "")),
                title=str(spec.get("title", "")),
                body=str(spec.get("_body", ""))[:1000],
                score=sc, sources=["jaccard"],
            ))

    for ent in bundle.get("entities", []):
        text_ = " ".join([str(ent.get("name", "")), str(ent.get("_body", ""))])
        sc = _jaccard_score(qtoks, text_)
        if sc > 0:
            hits.append(Hit(
                type="entity", ref=str(ent.get("slug", "")),
                title=str(ent.get("name", "")),
                body=str(ent.get("_body", ""))[:600],
                score=sc, sources=["jaccard"],
            ))

    for fact in bundle.get("facts", []):
        text_ = " ".join([str(fact.get("subject", "")), str(fact.get("_body", ""))])
        sc = _jaccard_score(qtoks, text_)
        if sc > 0:
            hits.append(Hit(
                type="fact", ref=str(fact.get("slug", "")),
                title=str(fact.get("subject", "")) or str(fact.get("slug", "")),
                body=str(fact.get("_body", ""))[:600],
                score=sc, sources=["jaccard"],
            ))

    for case in bundle.get("cases", []):
        text_ = " ".join([str(case.get("title", "")), str(case.get("_body", ""))])
        sc = _jaccard_score(qtoks, text_)
        if sc > 0:
            hits.append(Hit(
                type="case", ref=str(case.get("slug", "")),
                title=str(case.get("title", "")) or str(case.get("slug", "")),
                body=str(case.get("_body", ""))[:600],
                score=sc, sources=["jaccard"],
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

def _rrf_fuse(lists: list[tuple[str, list[Hit]]], top_k: int) -> list[Hit]:
    """对 N 路 rank list 做 Reciprocal Rank Fusion。

    每路:(source_name, hits)。source_name 用于补全 Hit.sources。
    rrf_score(item) = Σ over lists [ 1 / (k + rank_in_that_list) ]
    rank 从 1 开始;不在某个 list 里就不贡献。
    """
    fused: dict[tuple[str, str], Hit] = {}
    score: dict[tuple[str, str], float] = {}

    for source_name, hits in lists:
        for rank, h in enumerate(hits, start=1):
            key = (h.type, h.ref)
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
            if key not in fused:
                fused[key] = Hit(
                    type=h.type, ref=h.ref, title=h.title, body=h.body,
                    score=0.0, sources=list(h.sources),
                )
            if source_name not in fused[key].sources:
                fused[key].sources.append(source_name)

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

def _apply_feedback_weights(hits: list[Hit]) -> None:
    """加用户反馈信号:like/正向 emoji 加分,dislike/负向 emoji 扣分。失败静默。"""
    try:
        from helper.ask.feedback_signal import feedback_weights
        weights = feedback_weights()
    except Exception as e:  # noqa: BLE001
        log.warning("feedback_weights failed err=%s", e)
        return
    if not weights:
        return
    for h in hits:
        delta = weights.get((h.type, h.ref), 0.0)
        if delta:
            h.score += delta


def retrieve_relevant(
    question: str, *, top_k: int = 8, asker_domain: str = ""
) -> list[Hit]:
    """对 question 做检索,返 top_k Hit。

    Hybrid 三路:bundle Jaccard + FTS5 + 向量;任一路失败其它路仍正常。
    最后接用户反馈加权(ReactionLog → citations → spec/raw)。

    asker_domain 非空时, 出口跑 ACL 过滤:命中 topic_acl.yaml 但 asker 不在
    allowed_domains 的 hit 直接丢弃, LLM 看不到敏感原文。
    """
    qtoks = _tokens(question)
    if not qtoks:
        return []

    skip = _superseded_raw_ids()
    jac = _bundle_jaccard_pass(qtoks)
    fts_hits = _fts_pass(question, skip)
    vec = _vector_pass(question, skip)

    # 三路全空 → 没东西召回
    if not jac and not fts_hits and not vec:
        return []

    fused = _rrf_fuse(
        [("jaccard", jac), ("fts", fts_hits), ("vector", vec)],
        top_k * 4,
    )
    _apply_feedback_weights(fused)
    fused.sort(key=lambda h: -h.score)

    # ACL 出口过滤:asker 看不到的 topic 全过滤,然后再切 top_k
    if asker_domain:
        try:
            from helper.acl import filter_hits
            allowed, blocked = filter_hits(asker_domain, fused)
            if blocked:
                log.info("acl blocked %d/%d hits for asker=%s", len(blocked), len(fused), asker_domain)
            fused = allowed
        except Exception:  # noqa: BLE001
            log.exception("acl filter failed; default to allow all (caller still has deny_for_question gate)")

    return fused[:top_k]
