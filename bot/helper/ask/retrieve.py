"""从 bundle + sqlite FTS5 + 向量索引检索相关 spec / entity / section / decision / fact / case / relation。

M7 起 hybrid:
  - 路径 A:bundle 内存 Jaccard(几十~几百条 spec/entity 摘要,直接全扫便宜)
  - 路径 B:FTS5 + jieba 词面召回(候选表 + section/decision atom)
  - 路径 C:bge-m3 向量召回(对同义改写强;已晋升 spec/entity/fact/case/relation + section/decision)
  - 用 RRF (Reciprocal Rank Fusion, k=60) 融合所有路 rank,出 top_k

向量 / FTS 失败 → 各自路径降级返空,其余路仍正常工作。
为啥 bundle 还走 Jaccard:bundle 是已编译的小集合(内存),走 FTS 反而要先 jieba 切词
再 sqlite 一趟,得不偿失。

raw 不进主召回:raw 是原文层,知识已抽到 section/decision。raw 直接和 section
平权进 RRF 会让"知识已经结构化"这件事打折(粗粒度命中挤掉细粒度)。fts/vector
索引层仍保留 raw kind 不动 — 服务 reindex / 未来"原文回查"场景。
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
    L1Item,
    Memory,
    RawInput,
    SpecCandidate,
)

log = logging.getLogger(__name__)

RRF_K = 60  # RRF 论文默认值,大于命中数即可
JACCARD_TOP_K = 30
VECTOR_TOP_K = 30
FTS_TOP_K = 30

# section/decision 是细粒度召回主力,body 直接进 LLM 上下文。
SECTION_BODY_CAP = 1500


@dataclass
class Hit:
    type: str  # spec / section / decision / directive
    ref: str   # slug 或 "raw_id:idx" (atom)
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

# spec 是唯一的"raw → 候选"中间层。section/decision 在 l1_items 直接 ref raw_id,
# 不需要走候选反查。
_CANDIDATE_TABLES: list[tuple[type, str]] = [
    (SpecCandidate, "cluster_raw_ids_json"),
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

    策略:差集 = (任一已 superseded spec 支撑的 raw) − (任一仍 alive spec 支撑的 raw)。
    spec 是唯一的 raw 聚合产物 — section/decision 在 l1_items 直接 ref raw_id,
    不需要候选反查。
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


def _parse_l1_atom_ref(ref: str) -> tuple[int, int] | None:
    """ref 形如 "215:1" → (215, 1);非法格式返 None。"""
    parts = ref.split(":")
    if len(parts) != 2:
        return None
    if not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def _bot_reply_raw_ids(sess, raw_ids: set[int]) -> set[int]:
    """从 raw_ids 中筛出 source_type='im_wave_bot*' 的 raw —
    召回侧硬隔离: bot 自己的回复永远不该作为知识被引用。"""
    if not raw_ids:
        return set()
    rows = sess.execute(
        select(RawInput.id).where(
            RawInput.id.in_(raw_ids),
            RawInput.source_type.like("im_wave_bot%"),
        )
    ).scalars().all()
    return set(rows)


def _hydrate_l1_atoms(
    sess,
    kind: str,
    refs: list[str],
    skip_raw_ids: set[int],
    score_map: dict[tuple[str, str], float],
    source_name: str,
) -> list[Hit]:
    """从 l1_items 反查 section/decision 内容。

    section: title 当 Hit.title,body 进 Hit.body(SECTION_BODY_CAP cap)。
    decision: 拼 "scene → choice" 当 title,rationale + signals + tradeoffs 拼 body。
    raw 已在 skip 集合的全部丢弃(superseded)。
    bot 自答抽出的 atom (源 raw 是 im_wave_bot*) 整类丢弃 — 召回硬隔离。
    """
    parsed: list[tuple[str, int, int]] = []  # (orig_ref, raw_id, idx)
    for ref in refs:
        p = _parse_l1_atom_ref(ref)
        if p is None:
            continue
        raw_id, idx = p
        if raw_id in skip_raw_ids:
            continue
        parsed.append((ref, raw_id, idx))
    if not parsed:
        return []

    # 反查 raw_id 的 source_type, 整类丢弃 im_wave_bot
    bot_ids = _bot_reply_raw_ids(sess, {rid for _, rid, _ in parsed})
    if bot_ids:
        parsed = [(ref, rid, idx) for ref, rid, idx in parsed if rid not in bot_ids]
    if not parsed:
        return []

    pairs = [(rid, idx) for _, rid, idx in parsed]
    from sqlalchemy import or_, and_

    items = sess.execute(
        select(L1Item).where(
            L1Item.type == kind,
            or_(*[and_(L1Item.raw_id == rid, L1Item.idx == ix) for rid, ix in pairs]),
        )
    ).scalars().all()
    item_by_key = {(it.raw_id, it.idx): it for it in items}

    out: list[Hit] = []
    import json as _json
    for ref, raw_id, idx in parsed:
        it = item_by_key.get((raw_id, idx))
        if it is None:
            continue
        try:
            payload = _json.loads(it.payload_json or "{}")
        except _json.JSONDecodeError:
            continue
        if kind == "section":
            title = (payload.get("title") or "").strip() or f"section#{ref}"
            body = (payload.get("body") or "").strip()
            if len(body) > SECTION_BODY_CAP:
                body = body[:SECTION_BODY_CAP] + "…"
        else:  # decision
            scene = (payload.get("scene") or "").strip()
            choice = (payload.get("choice") or "").strip()
            title = f"{scene} → {choice}".strip(" →") or f"decision#{ref}"
            body_parts = []
            if payload.get("rationale"):
                body_parts.append(f"理由: {payload['rationale']}")
            if payload.get("signals"):
                sigs = payload["signals"]
                if isinstance(sigs, list):
                    body_parts.append("信号: " + " / ".join(str(x) for x in sigs))
            if payload.get("tradeoffs"):
                tos = payload["tradeoffs"]
                if isinstance(tos, list):
                    body_parts.append("取舍: " + " / ".join(str(x) for x in tos))
            body = "\n".join(body_parts)
            if len(body) > SECTION_BODY_CAP:
                body = body[:SECTION_BODY_CAP] + "…"
        out.append(Hit(
            type=kind, ref=ref, title=title, body=body,
            score=score_map[(kind, ref)], sources=[source_name],
        ))
    return out


def _hydrate_fts_hits(
    fts_hits: list[tuple[str, str, float]],
    skip_raw_ids: set[int],
) -> list[Hit]:
    """fts.search 给的 (kind, ref, score) 反查正文,组成 Hit。

    raw kind 命中直接丢弃 — raw 是原文层,知识已抽到 section/decision,主召回不出 raw。
    spec  → 优先 bundle(已 review),没有再回 SpecCandidate
    section/decision → l1_items 反查
    bundle 命中的 spec 这里也会被反查到,但 _bundle_jaccard_pass 也会出
    同 (type, ref),由 RRF 融合层去重。
    """
    if not fts_hits:
        return []

    # 按 kind 拆 ref 列表; raw kind 直接丢
    by_kind: dict[str, list[str]] = {}
    score_map: dict[tuple[str, str], float] = {}
    for kind, ref, score in fts_hits:
        if kind == "raw":
            continue
        if kind in ("section", "decision"):
            p = _parse_l1_atom_ref(ref)
            if p is None or p[0] in skip_raw_ids:
                continue
        by_kind.setdefault(kind, []).append(ref)
        score_map[(kind, ref)] = score

    out: list[Hit] = []
    with session() as s:
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
        if "section" in by_kind:
            out.extend(_hydrate_l1_atoms(
                s, "section", by_kind["section"], skip_raw_ids, score_map, "fts",
            ))
        if "decision" in by_kind:
            out.extend(_hydrate_l1_atoms(
                s, "decision", by_kind["decision"], skip_raw_ids, score_map, "fts",
            ))
    return out


def _fts_pass(question: str, skip_raw_ids: set[int]) -> list[Hit]:
    """FTS5 + jieba 词面召回,覆盖 spec / section / decision。"""
    try:
        with session() as s:
            hits = fts.search(s, query=question, top_k=FTS_TOP_K)
    except Exception as e:  # noqa: BLE001
        log.warning("fts_pass failed err=%s", e)
        return []
    return _hydrate_fts_hits(hits, skip_raw_ids)


def _directive_pass(question: str) -> list[Hit]:
    """alive directive 全捞, 题面 token 与 directive 文本 token 有交集即命中。

    directive 是"行为指令"不是"事实", 优先级最高 — 不和 raw/section 走 bm25
    抢 RRF top_k(directive 文本短, bm25 天然吃亏被压出榜)。 也不进 fts_items
    表, 不需要 upsert/delete/reindex 同步。 alive directive 全公司量级稳定在
    百条以内, jieba 切词遍历毫秒级, 不需要索引。
    """
    qtoks = _tokens(question)
    if not qtoks:
        return []
    out: list[Hit] = []
    with session() as s:
        mems = s.execute(
            select(Memory).where(Memory.superseded_at.is_(None)).order_by(Memory.id)
        ).scalars().all()
    for m in mems:
        text = m.directive or ""
        if not text.strip():
            continue
        if not (qtoks & _tokens(text)):
            continue
        title = (
            f"directive 涉及『{m.scope_ref}』"
            if m.scope_type == "entity" and m.scope_ref
            else "directive(global)"
        )
        out.append(Hit(
            type="directive", ref=str(m.id), title=title, body=text,
            score=1.0, sources=["directive"],
        ))
    return out


# ---------- bundle 内存 Jaccard 路径(只扫已编译的小集合) ----------

def _bundle_jaccard_pass(qtoks: set[str]) -> list[Hit]:
    """bundle 是已编译的 spec 摘要(几十~几百条),内存全扫便宜。

    section/decision 由 _fts_pass / _vector_pass 召回,不进 bundle。
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

    hits.sort(key=lambda h: -h.score)
    return hits[:JACCARD_TOP_K]


# ---------- 向量路径 ----------

def _hydrate_vector_hits(
    vec_hits: list[vector.VectorHit], skip_raw_ids: set[int]
) -> list[Hit]:
    """vec0 KNN 给的是 (kind, ref, distance);需要把对应 spec / section / decision
    的标题 + 正文取出来。

    raw kind 命中直接丢弃 — raw 不进主召回。
    spec 走 bundle(已编译好,正文可读);
    section/decision 走 l1_items 反查,与 fts 路径共用 _hydrate_l1_atoms。
    """
    if not vec_hits:
        return []

    bundle = load_bundle()
    spec_by_slug = {str(sp.get("slug", "")): sp for sp in bundle.get("specs", [])}

    out: list[Hit] = []

    # 收集 section/decision refs + 它们的 score(用于 _hydrate_l1_atoms 复用)
    atom_refs_by_kind: dict[str, list[str]] = {"section": [], "decision": []}
    atom_score_map: dict[tuple[str, str], float] = {}

    # vector 路径距离越小越相关。融合用的是 rank,score 这里只为可读性留 1/(1+dist)
    for h in vec_hits:
        readable = 1.0 / (1.0 + h.distance)
        if h.kind in ("section", "decision"):
            atom_refs_by_kind[h.kind].append(h.ref)
            atom_score_map[(h.kind, h.ref)] = readable
            continue
        if h.kind == "raw":
            continue  # raw 不进主召回
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

    # section / decision 复用 _hydrate_l1_atoms(同一份 l1_items 反查 + body cap 逻辑)
    if atom_refs_by_kind["section"] or atom_refs_by_kind["decision"]:
        with session() as s:
            for kind in ("section", "decision"):
                refs = atom_refs_by_kind[kind]
                if not refs:
                    continue
                out.extend(_hydrate_l1_atoms(
                    s, kind, refs, skip_raw_ids, atom_score_map, "vector",
                ))
    return out


def _vector_pass(question: str, skip_raw_ids: set[int]) -> list[Hit]:
    try:
        with session() as s:
            vec_hits = vector.search(s, query=question, top_k=VECTOR_TOP_K)
    except Exception as e:  # noqa: BLE001
        log.warning("vector_pass failed err=%s", e)
        return []
    return _hydrate_vector_hits(vec_hits, skip_raw_ids)


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

    # spec 维持"权重高"语义,fact/case 与 entity / section / decision 同档
    for key, h in fused.items():
        s = score[key]
        if h.type == "spec":
            s *= 1.5
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
    # directive 独立通道, 不进 RRF, 命中即拼到 prompt 用户偏好段
    directives = _directive_pass(question)

    # 全空 → 没东西召回
    if not jac and not fts_hits and not vec and not directives:
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

    # directive 不切 top_k(独立通道, 命中即拼); 事实段 top_k 后再 append
    return fused[:top_k] + directives
