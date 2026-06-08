"""改动 3: SpecTopic 语义聚类 — bge-m3 embedding + 余弦阈值 0.78。

替代旧的 entity 共现聚类。 旧版按 raw_id × entity 字符串桥接易把无关
decision 串到一起 (例如同一会议两条决策共享发言人却无业务相关性);
embedding 聚类按"决策语义相近"分簇, 准确率显著提升, 后续触发 draft 也
能按 SpecTopic.last_promoted_at 节流避免重复打扰 owner。

主入口:
- assign_topic(raw_id, idx) — 单条 decision 落 topic (异步消费)
- scan_topics_for_draft() — 周期性扫所有 topic, 返回该触发 draft 的 topic_id 列表

阈值 0.78 来自 bge-m3 + 中文短文本 (<200 字) 经验值, 后续按 backfill
出来的 topic 分布微调。
"""

from __future__ import annotations

import json
import logging
import struct
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import L1Item, RawInput, SpecTopic

log = logging.getLogger(__name__)

# 余弦阈值: ≥ 0.78 视为同 topic, 否则新建。
_TOPIC_THRESHOLD = 0.78

# 静默期: 簇最新 decision 距今 ≥ 90 天 + 没 promote 过 → 强触发 draft。
_SILENT_DAYS = 90

# 防抖: 已 promote 过的 topic, 30 天内不重新触发 draft。
_REPROMOTE_COOLDOWN_DAYS = 30


_DIM = 1024
_BYTES = _DIM * 2


def _decode(blob: bytes | None) -> list[float] | None:
    if not blob or len(blob) != _BYTES:
        return None
    return list(struct.unpack(f"{_DIM}e", blob))


def _encode(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}e", *vec)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    import math
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _update_centroid(old: list[float], n: int, new_vec: list[float]) -> list[float]:
    """增量平均: new_centroid = (old * n + new_vec) / (n + 1)。
    n 是合入前的成员数 — old 已经是这 n 个的平均。"""
    if n <= 0:
        return list(new_vec)
    return [(old[i] * n + new_vec[i]) / (n + 1) for i in range(len(old))]


def assign_topic(raw_id: int, idx: int) -> int | None:
    """单条 decision (raw_id, idx) 归 topic。

    流程:
    1. 拉这条 L1Item.embedding; 空 → 不归簇返 None
    2. 全表扫 SpecTopic 算余弦, 取最大
    3. score ≥ 0.78 → 合入旧 topic, 增量更新 centroid + decision_count + last_updated
       否则 → 新建 topic, centroid = 该 emb, count = 1
    4. 写回 L1Item.topic_id
    5. 返回 topic_id (None 仅在 embedding 缺失时)

    并发: 简单实现, 暂不上锁 — 主链路 sink 后异步消费, 多实例同时跑会偶发
    重复合入但不会丢数据 (centroid 漂移可接受, 不是关键字段)。 后续上分布式
    锁再说。
    """
    with session() as s:
        item = s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id, L1Item.idx == idx)
        ).scalar_one_or_none()
        if item is None or item.type != "decision":
            return None
        new_vec = _decode(item.embedding)
        if new_vec is None:
            log.debug("assign_topic skip: no embedding raw=%s idx=%s", raw_id, idx)
            return None

        topics = list(s.execute(
            select(SpecTopic.id, SpecTopic.centroid, SpecTopic.decision_count)
        ).all())
        best_id: int | None = None
        best_score = 0.0
        for tid, blob, _cnt in topics:
            cand = _decode(blob)
            if cand is None:
                continue
            sc = _cosine(new_vec, cand)
            if sc > best_score:
                best_score = sc
                best_id = tid

        now = datetime.now(timezone.utc)
        if best_id is not None and best_score >= _TOPIC_THRESHOLD:
            topic = s.get(SpecTopic, best_id)
            old_vec = _decode(topic.centroid) or new_vec
            updated = _update_centroid(old_vec, topic.decision_count, new_vec)
            topic.centroid = _encode(updated)
            topic.decision_count = topic.decision_count + 1
            topic.last_updated = now
            item.topic_id = topic.id
            s.commit()
            log.info(
                "assign_topic merged raw=%s idx=%s → topic=%s score=%.3f count=%d",
                raw_id, idx, topic.id, best_score, topic.decision_count,
            )
            return topic.id

        new_topic = SpecTopic(
            centroid=_encode(new_vec),
            decision_count=1,
            last_updated=now,
        )
        s.add(new_topic)
        s.flush()
        item.topic_id = new_topic.id
        s.commit()
        log.info("assign_topic new raw=%s idx=%s → topic=%s", raw_id, idx, new_topic.id)
        return new_topic.id


def assign_topic_for_raw(raw_id: int) -> list[int]:
    """raw 内所有 decision 都归簇。 sink 跑完 L1 后调用。"""
    with session() as s:
        rows = s.execute(
            select(L1Item.idx).where(
                L1Item.raw_id == raw_id, L1Item.type == "decision",
            )
        ).all()
    out = []
    for (idx,) in rows:
        tid = assign_topic(raw_id, idx)
        if tid is not None:
            out.append(tid)
    return out


def topic_keys(topic_id: int) -> list[tuple[int, int]]:
    """拿 topic 下所有 (raw_id, idx) 元组。 给 draft_spec_from_topic 用。"""
    with session() as s:
        rows = s.execute(
            select(L1Item.raw_id, L1Item.idx)
            .where(L1Item.topic_id == topic_id)
            .where(L1Item.type == "decision")
            .order_by(L1Item.raw_id, L1Item.idx)
        ).all()
    return [(r, i) for r, i in rows]


def _topic_latest_raw_age_days(keys: list[tuple[int, int]]) -> int:
    """簇里最新 raw 的距今天数, 老 raw 数据 created_at 可能 naive 视为 UTC。"""
    if not keys:
        return 0
    raw_ids = list({k[0] for k in keys})
    with session() as s:
        rows = s.execute(
            select(RawInput.created_at).where(RawInput.id.in_(raw_ids))
        ).all()
    times = [r[0] for r in rows if r[0] is not None]
    if not times:
        return 0
    latest = max(times)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - latest).days)


def scan_topics_for_draft() -> list[int]:
    """扫所有 SpecTopic, 返回当前应触发 draft 的 topic_id 列表。

    判据 (任一即可, 与 draft.py 改动 4 一致):
    - 普适: 由调用方 (draft_spec_from_topic) 用 LLM 判,这里无法预判 — 该路径
            的"触发"靠 saturation/silence 走完一遍后, 调用方再独立判普适。
            scan 这层只过结构性判据, 不算 LLM。
    - 饱和: decision_count ≥ 3 且未 promote 过 (或 promote 过但已过 30 天冷却)
    - 静默: latest raw 距今 ≥ 90 天 且未 promote 过

    "未 promote 过" 含义: last_promoted_at 为空, 或距今 ≥ 30 天 (允许重 draft)。
    """
    now = datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(days=_REPROMOTE_COOLDOWN_DAYS)
    out: list[int] = []
    with session() as s:
        topics = list(s.execute(
            select(SpecTopic.id, SpecTopic.decision_count, SpecTopic.last_promoted_at)
        ).all())
    for tid, count, last_promoted in topics:
        if last_promoted is not None:
            if last_promoted.tzinfo is None:
                last_promoted = last_promoted.replace(tzinfo=timezone.utc)
            if last_promoted >= cooldown_cutoff:
                # 仍在冷却期, 不重触发
                continue
        if count >= 3:
            out.append(tid)
            continue
        # 静默期: 看该 topic 下最新 raw 年龄
        keys = topic_keys(tid)
        if _topic_latest_raw_age_days(keys) >= _SILENT_DAYS:
            out.append(tid)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 兼容旧调用方: server.py / cli.py 的 specgen run 入口曾返 cluster 列表的列表。
# 改动 3 后改成 "扫 topic 返回 keys 列表" 的形式, 调用方语义不变 (仍是"对每簇
# 调 draft"), 内部改走 SpecTopic。
# ─────────────────────────────────────────────────────────────────────────────


def cluster_l1_results(*, min_cluster_size: int = 2) -> list[list[tuple[int, int]]]:
    """[改动 3 兼容层] 返回 (raw_id, idx) 元组列表的列表。

    新语义: 扫 SpecTopic 表, 把每个该触发 draft 的 topic 转成 keys 列表。
    min_cluster_size 仍生效 — 簇成员数 < min_cluster_size 直接过滤掉
    (避免老调用方在静默期触发的 1 条簇上跑 draft, 由调用方决定要不要看)。
    """
    out: list[list[tuple[int, int]]] = []
    for tid in scan_topics_for_draft():
        keys = topic_keys(tid)
        if len(keys) >= min_cluster_size:
            out.append(keys)
    return out
