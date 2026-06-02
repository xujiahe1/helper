"""SQLite FTS5 词面召回 — 替代 retrieve.py 早先的 Jaccard 全表扫。

设计:
- 一张共表 `fts_items(text, kind UNINDEXED, ref UNINDEXED)`(在 db.py 里建)。
  kind ∈ {raw, spec, entity, fact, case, relation};ref 对应的 slug / raw_id 字符串。
- 入库:Python 端 jieba 切词 → 空格拼字符串 → 写 fts5。
  jieba 在中文分词上对领域名词友好(比 unicode61 的"按 Unicode 类别分词"准很多)。
- 召回:`SELECT kind, ref, bm25(fts_items) FROM fts_items WHERE fts_items MATCH ?`
  query 同样 jieba 切词后空格拼,fts5 把空格当 OR-able token 序列。
- 写入侧失败 log 不抛 — 召回侧降级,不阻塞主流程(召回少一个来源不致命,
  写入时 raise 会让 ingest 整条断链)。

为啥用一张共表:
- 业务上 retrieve / detector 都按 kind 过滤,过滤+ MATCH 在 fts5 里靠 WHERE kind=?
  + UNINDEXED 列上的过滤,几万行规模性能没差;
- 部署时只起一张虚拟表,backfill / migration 简单。
"""

from __future__ import annotations

import logging
import re
import threading

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


_jieba_lock = threading.Lock()
_jieba_loaded = False


def _ensure_jieba() -> None:
    """首次调用时加载 jieba 字典(~1s)。多线程安全,只跑一次。"""
    global _jieba_loaded
    if _jieba_loaded:
        return
    with _jieba_lock:
        if _jieba_loaded:
            return
        import jieba

        jieba.initialize()
        # 静音 jieba 自身的 INFO 日志
        jieba.setLogLevel(logging.WARNING)
        _jieba_loaded = True


# fts5 query 里 ! ^ + - * : " ( ) 是元字符,用普通 token 召回时要避开
# 中英混合下最稳是把所有非字母数字非 CJK 字符全替成空格。
_NON_TOKEN_RE = re.compile(r"[^\w一-鿿]+")
# fts5 单 token 最大 1000 字符;实际我们切词后远小于,留一道 cap 以防极端情况
_MAX_TOKEN_LEN = 64


def tokenize(content: str) -> str:
    """jieba 切词 → 空格拼。空字符串安全。"""
    if not content:
        return ""
    _ensure_jieba()
    import jieba

    # 先把元字符替成空格,再切;否则 jieba 会把含 - 的串当一个 token 出来,
    # MATCH 时 fts5 又把 - 当负向操作符,query 端抛错。
    cleaned = _NON_TOKEN_RE.sub(" ", content)
    tokens: list[str] = []
    for tok in jieba.cut_for_search(cleaned):
        tok = tok.strip()
        if not tok:
            continue
        if len(tok) > _MAX_TOKEN_LEN:
            tok = tok[:_MAX_TOKEN_LEN]
        tokens.append(tok)
    return " ".join(tokens)


def _build_match_query(query: str) -> str:
    """query 切词后用 OR 连接 — 任一 token 命中即为候选,bm25 排序自然处理多 token 加权。
    AND 模式对中文 query 太严(漏掉同义改写)。
    """
    if not query.strip():
        return ""
    _ensure_jieba()
    import jieba

    cleaned = _NON_TOKEN_RE.sub(" ", query)
    raw_tokens = [t.strip() for t in jieba.cut_for_search(cleaned)]
    # token 长度 < 2 的 ASCII 单字符过滤掉(噪音);中文单字保留(领域名词单字
    # 命中率不可忽略,如"组""池")
    tokens: list[str] = []
    for t in raw_tokens:
        if not t:
            continue
        if t.isascii() and len(t) < 2:
            continue
        if len(t) > _MAX_TOKEN_LEN:
            t = t[:_MAX_TOKEN_LEN]
        # fts5 phrase 用双引号包,确保特殊字符(如 _)不被当操作符
        tokens.append(f'"{t}"')
    if not tokens:
        return ""
    return " OR ".join(tokens)


# ---------- 写入 ----------

def upsert(sess: Session, *, kind: str, ref: str, content: str) -> None:
    """同 (kind, ref) 已有 → 先 DELETE 再 INSERT。fts5 没有 UPSERT。

    失败仅 log,不抛。
    """
    if not kind or not ref:
        return
    try:
        tokenized = tokenize(content or "")
        sess.execute(
            text("DELETE FROM fts_items WHERE kind = :k AND ref = :r"),
            {"k": kind, "r": ref},
        )
        if tokenized:
            sess.execute(
                text("INSERT INTO fts_items(text, kind, ref) VALUES(:t, :k, :r)"),
                {"t": tokenized, "k": kind, "r": ref},
            )
    except Exception:  # noqa: BLE001
        log.exception("fts.upsert failed kind=%s ref=%s", kind, ref)


def delete(sess: Session, *, kind: str, ref: str) -> None:
    """supersede / DELETE 候选时调。失败仅 log。"""
    if not kind or not ref:
        return
    try:
        sess.execute(
            text("DELETE FROM fts_items WHERE kind = :k AND ref = :r"),
            {"k": kind, "r": ref},
        )
    except Exception:  # noqa: BLE001
        log.exception("fts.delete failed kind=%s ref=%s", kind, ref)


def clear_all(sess: Session) -> None:
    """rebuild 前清场。"""
    sess.execute(text("DELETE FROM fts_items"))


# ---------- 召回 ----------

def search(
    sess: Session,
    *,
    query: str,
    top_k: int = 30,
    kinds: list[str] | None = None,
) -> list[tuple[str, str, float]]:
    """对 query 做 fts5 + bm25 召回,返回 [(kind, ref, score), ...]。

    score 是 bm25,fts5 实现里 bm25 越小越相关(类似距离);这里取负值让"score
    越大越好",方便后续 RRF 融合时统一语义。

    kinds=None 不过滤(全 kind);kinds=['raw'] 只要 raw。

    召回失败 → log + 空列表。
    """
    q = _build_match_query(query)
    if not q:
        return []
    sql = (
        "SELECT kind, ref, bm25(fts_items) AS s "
        "FROM fts_items WHERE fts_items MATCH :q"
    )
    params: dict = {"q": q}
    if kinds:
        # IN 列表绑定走 expanding bindparam;这里手拼几个占位避免引 sqlalchemy.bindparam
        placeholders = ",".join(f":k{i}" for i in range(len(kinds)))
        sql += f" AND kind IN ({placeholders})"
        for i, k in enumerate(kinds):
            params[f"k{i}"] = k
    sql += " ORDER BY s LIMIT :lim"
    params["lim"] = max(1, top_k)
    try:
        rows = sess.execute(text(sql), params).all()
    except Exception:  # noqa: BLE001
        log.exception("fts.search failed query=%r", query[:80])
        return []
    return [(r[0], r[1], -float(r[2])) for r in rows]


# ---------- 高层 index_* — 跟 vector.py 一致的对外形状 ----------

def index_raw(sess: Session, raw_id: int) -> None:
    """raw + L1 atoms 拼检索文本写 fts。L1 失败 / filtered 不写。"""
    import json as _json

    from sqlalchemy import select as _select

    from helper.storage.models import L1Item, L1Result, RawInput

    raw = sess.get(RawInput, raw_id)
    if raw is None:
        return
    l1 = sess.get(L1Result, raw_id)
    if l1 is None or l1.error:
        return
    parts = [raw.content_text or ""]
    items = sess.execute(
        _select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
    ).scalars().all()
    for it in items:
        try:
            payload = _json.loads(it.payload_json or "{}")
        except _json.JSONDecodeError:
            continue
        for k, v in payload.items():
            if k.endswith("_raw_ids") or k.endswith("_speaker"):
                continue
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                for el in v:
                    if isinstance(el, str) and el.strip():
                        parts.append(el.strip())
    upsert(sess, kind="raw", ref=str(raw_id), content="\n".join(parts))


def index_l1_atom(sess: Session, raw_id: int, idx: int) -> None:
    """L1Item(section / decision)接 fts。ref="{raw_id}:{idx}"。

    section: title + body + topics 全文索引(body 直接进,不截 — fts 只切词,不存原文)
    decision: scene + signals + tradeoffs + choice + rationale 拍平进索引
    其他类型 (v1 fact/case/concept/relation) 走各自 candidate 表的 indexer,这里不重复。
    """
    import json as _json

    from sqlalchemy import select as _select

    from helper.storage.models import L1Item

    item = sess.execute(
        _select(L1Item).where(L1Item.raw_id == raw_id, L1Item.idx == idx)
    ).scalar_one_or_none()
    if item is None or item.type not in ("section", "decision"):
        return
    try:
        payload = _json.loads(item.payload_json or "{}")
    except _json.JSONDecodeError:
        return

    parts: list[str] = []
    for k, v in payload.items():
        if k.endswith("_raw_ids") or k.endswith("_speaker") or k == "primary_raw_id":
            continue
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
        elif isinstance(v, list):
            for el in v:
                if isinstance(el, str) and el.strip():
                    parts.append(el.strip())
    upsert(
        sess,
        kind=item.type,
        ref=f"{raw_id}:{idx}",
        content="\n".join(parts),
    )


def delete_l1_atoms_for_raw(sess: Session, raw_id: int) -> None:
    """rebuild raw 时清掉所有 (section|decision):raw_id:* 旧索引。"""
    try:
        sess.execute(
            text(
                "DELETE FROM fts_items WHERE kind IN ('section','decision') "
                "AND ref LIKE :p"
            ),
            {"p": f"{raw_id}:%"},
        )
    except Exception:  # noqa: BLE001
        log.exception("fts.delete_l1_atoms_for_raw failed raw_id=%s", raw_id)


def index_spec(sess: Session, slug: str) -> None:
    from sqlalchemy import select as _select
    from helper.storage.models import SpecCandidate

    sc = sess.execute(
        _select(SpecCandidate).where(SpecCandidate.slug == slug)
    ).scalar_one_or_none()
    if sc is None:
        return
    parts = [sc.title or "", sc.statement or "", sc.rationale or ""]
    upsert(sess, kind="spec", ref=slug, content="\n".join(parts))


def index_entity(sess: Session, slug: str) -> None:
    from sqlalchemy import select as _select
    from helper.storage.models import EntityCandidate

    ec = sess.execute(
        _select(EntityCandidate).where(EntityCandidate.slug == slug)
    ).scalar_one_or_none()
    if ec is None:
        return
    parts = [ec.name or "", ec.description or ""]
    upsert(sess, kind="entity", ref=slug, content="\n".join(parts))


def index_fact(sess: Session, slug: str) -> None:
    from sqlalchemy import select as _select
    from helper.storage.models import FactCandidate

    fc = sess.execute(
        _select(FactCandidate).where(FactCandidate.slug == slug)
    ).scalar_one_or_none()
    if fc is None:
        return
    parts = [
        fc.subject or "",
        fc.predicate or "",
        fc.object or "",
        fc.scope or "",
        fc.statement or "",
    ]
    upsert(sess, kind="fact", ref=slug, content=" ".join(parts))


def index_case(sess: Session, slug: str) -> None:
    from sqlalchemy import select as _select
    from helper.storage.models import CaseCandidate

    cc = sess.execute(
        _select(CaseCandidate).where(CaseCandidate.slug == slug)
    ).scalar_one_or_none()
    if cc is None:
        return
    parts = [
        cc.title or "",
        cc.scene or "",
        cc.what_happened or "",
        cc.outcome or "",
    ]
    upsert(sess, kind="case", ref=slug, content=" ".join(parts))


def index_relation(sess: Session, slug: str) -> None:
    from sqlalchemy import select as _select
    from helper.storage.models import RelationCandidate

    rc = sess.execute(
        _select(RelationCandidate).where(RelationCandidate.slug == slug)
    ).scalar_one_or_none()
    if rc is None:
        return
    parts = [
        rc.entity_a or "",
        rc.relation or "",
        rc.entity_b or "",
        rc.description or "",
    ]
    upsert(sess, kind="relation", ref=slug, content=" ".join(parts))
