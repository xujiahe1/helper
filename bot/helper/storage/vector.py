"""vec_items + VectorIndex 的 CRUD。

设计参见 helper/storage/db.py 顶部说明:
- vec_items (vec0 虚拟表):rowid + embedding(1024 维 float)
- VectorIndex (普通 ORM 表):rowid → (kind, ref, content_hash, model, indexed_at)
- (kind, ref) 唯一,upsert 行为:同 ref 同 hash 同 model → no-op;不同 hash/model → 重 embed + 更新 vec_items

vec0 不支持任意普通列;过滤 (kind / ref) 走 sidecar JOIN 而非 vec0 内置 WHERE。
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from helper.llm.embed import embed, embed_model, embed_one
from helper.storage.db import VEC_DIM
from helper.storage.models import VectorIndex

log = logging.getLogger(__name__)


# ---------- 序列化 ----------

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pack(vec: list[float]) -> bytes:
    """vec0 接受紧凑 float32 字节。Python list[float] 是 64 位,这里压成 32 位。"""
    if len(vec) != VEC_DIM:
        raise ValueError(f"embedding dim mismatch: got {len(vec)}, want {VEC_DIM}")
    return struct.pack(f"{VEC_DIM}f", *vec)


# ---------- CRUD ----------

def upsert(
    sess: Session,
    *,
    kind: str,
    ref: str,
    content: str,
) -> int | None:
    """若 (kind, ref) 已存在且 hash + model 都没变 → no-op,返回旧 rowid;
    否则重新 embed + 写入 vec_items + 更新 sidecar,返回新/更新后的 rowid。

    embed 失败 → log + 返 None,让调用方继续其它工作不阻塞主流程。
    """
    h = _content_hash(content or "")
    model = embed_model()
    existing = sess.execute(
        select(VectorIndex).where(VectorIndex.kind == kind, VectorIndex.ref == ref)
    ).scalar_one_or_none()

    if existing is not None and existing.content_hash == h and existing.model == model:
        return existing.rowid

    try:
        vec = embed_one(content or " ")
    except Exception as e:  # noqa: BLE001
        log.warning("vector.upsert embed failed kind=%s ref=%s err=%s", kind, ref, e)
        return None
    blob = _pack(vec)

    if existing is None:
        # vec_items 是虚拟表,SQLAlchemy ORM 无法管理 → 走原生 SQL 拿回 last_insert_rowid()
        result = sess.execute(
            text("INSERT INTO vec_items(embedding) VALUES(:e)"),
            {"e": blob},
        )
        rowid = result.lastrowid
        sess.add(
            VectorIndex(
                rowid=rowid,
                kind=kind,
                ref=ref,
                content_hash=h,
                model=model,
            )
        )
    else:
        sess.execute(
            text("UPDATE vec_items SET embedding = :e WHERE rowid = :r"),
            {"e": blob, "r": existing.rowid},
        )
        existing.content_hash = h
        existing.model = model
        from helper.storage.models import _utcnow  # 局部导入避开循环
        existing.indexed_at = _utcnow()
        rowid = existing.rowid

    return rowid


def delete(sess: Session, *, kind: str, ref: str) -> bool:
    """删除一条索引(对象被废弃 / reindex 前清场用)。"""
    existing = sess.execute(
        select(VectorIndex).where(VectorIndex.kind == kind, VectorIndex.ref == ref)
    ).scalar_one_or_none()
    if existing is None:
        return False
    sess.execute(text("DELETE FROM vec_items WHERE rowid = :r"), {"r": existing.rowid})
    sess.delete(existing)
    return True


def clear_all(sess: Session) -> None:
    """全清(reindex 前用)。"""
    sess.execute(text("DELETE FROM vec_items"))
    sess.execute(text("DELETE FROM vector_index"))


# ---------- 查询 ----------

@dataclass
class VectorHit:
    kind: str
    ref: str
    distance: float  # vec0 默认 L2 距离,越小越近


def search(
    sess: Session,
    *,
    query: str,
    top_k: int = 20,
    kinds: list[str] | None = None,
) -> list[VectorHit]:
    """对 query 做 KNN 召回。

    vec0 KNN 语法:`WHERE embedding MATCH :q AND k = :k`,k 必须 ≥ 1。
    vec0 自带的 WHERE 子句不能 JOIN,所以策略是:
      1. 先 vec_items KNN 取 top_k * 2(留余量给 kind 过滤)
      2. 再 sidecar 过滤 + 截断 top_k

    embed 失败 → 返空列表(查询路径降级到只走 Jaccard,见 helper.ask.retrieve)。
    """
    if not query.strip():
        return []
    try:
        qvec = embed_one(query)
    except Exception as e:  # noqa: BLE001
        log.warning("vector.search embed failed err=%s", e)
        return []
    qblob = _pack(qvec)

    fetch_k = max(top_k * 2, top_k)
    rows = sess.execute(
        text(
            "SELECT rowid, distance FROM vec_items "
            "WHERE embedding MATCH :q AND k = :k "
            "ORDER BY distance"
        ),
        {"q": qblob, "k": fetch_k},
    ).all()
    if not rows:
        return []

    rowid_to_dist = {r[0]: r[1] for r in rows}
    metas = sess.execute(
        select(VectorIndex).where(VectorIndex.rowid.in_(list(rowid_to_dist)))
    ).scalars().all()

    hits: list[VectorHit] = []
    for m in metas:
        if kinds is not None and m.kind not in kinds:
            continue
        hits.append(VectorHit(kind=m.kind, ref=m.ref, distance=float(rowid_to_dist[m.rowid])))
    hits.sort(key=lambda h: h.distance)
    return hits[:top_k]


# ---------- 高层入口:对应 raw / spec / entity 三种 kind 的 index ----------

def index_raw(sess: Session, raw_id: int) -> int | None:
    """raw + L1 合并 index。L1 没跑 / 失败 / filtered 的不索引(信息含量太低,占位也无意义)。

    L1 内容来自 L1Item.payload_json — 把每条 atom 的 payload 值拍平拼进去,
    decision 的 scene/choice/rationale + fact 的 subject/predicate/object + ... 都进索引。
    """
    import json as _json

    from helper.storage.models import L1Item, L1Result, RawInput

    raw = sess.get(RawInput, raw_id)
    if raw is None:
        return None
    l1 = sess.get(L1Result, raw_id)
    if l1 is None or l1.error:
        # 没成功跑过 L1 的 raw 不进向量库 — 包括 filtered:* 群聊噪音
        return None

    parts: list[str] = [raw.content_text or ""]
    items = sess.execute(
        select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
    ).scalars().all()
    for it in items:
        try:
            payload = _json.loads(it.payload_json or "{}")
        except _json.JSONDecodeError:
            continue
        # payload 各字段值都喂进索引(忽略 source_raw_ids 这种 id 引用)
        for k, v in payload.items():
            if k.endswith("_raw_ids") or k.endswith("_speaker"):
                continue
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                for el in v:
                    if isinstance(el, str) and el.strip():
                        parts.append(el.strip())
    content = "\n".join(p for p in parts if p)
    return upsert(sess, kind="raw", ref=str(raw_id), content=content)


def index_spec(sess: Session, slug: str) -> int | None:
    """spec 落 git 后调。从 SpecCandidate 取 title + statement + rationale 拼。"""
    from helper.storage.models import SpecCandidate

    sc = sess.execute(
        select(SpecCandidate).where(SpecCandidate.slug == slug)
    ).scalar_one_or_none()
    if sc is None:
        return None
    parts = [sc.title or "", sc.statement or "", sc.rationale or ""]
    content = "\n".join(p for p in parts if p)
    return upsert(sess, kind="spec", ref=slug, content=content)


def index_entity(sess: Session, slug: str) -> int | None:
    """entity 晋升 / 刷新后调。"""
    from helper.storage.models import EntityCandidate

    ec = sess.execute(
        select(EntityCandidate).where(EntityCandidate.slug == slug)
    ).scalar_one_or_none()
    if ec is None:
        return None
    parts = [ec.name or "", ec.description or ""]
    content = "\n".join(p for p in parts if p)
    return upsert(sess, kind="entity", ref=slug, content=content)
