"""改动 3 上线前 backfill — SpecTopic 语义聚类。

**阻塞上线**: 不跑 → 存量 decision 全部 topic_id=None → scan_topics_for_draft
找不到任何簇 → 主链路 daily 扫描相当于不工作。

幂等步骤:
1. 删除所有 review_status='pending' 且 promoted_at 为空的 SpecCandidate
   (改动 3 改了聚簇逻辑 + slug 由新 cluster 决定, 旧 pending 无意义)
   approved 的不动 — 改动 5 防覆写已经把它们和新草稿隔离。
2. 扫所有 type='decision' 且 embedding 为空的 L1Item, 算 embedding 写回。
3. 清空 SpecTopic + 重置所有 L1Item.topic_id 为 None。
4. 按 created_at 升序逐条 assign_topic, 重建 SpecTopic 簇。
5. 干跑 scan_topics_for_draft, log 出结果但**不实际 draft**, 让 owner 决定何时启。

跑法:
    cd bot/
    .venv/bin/python -m scripts.backfill_spec_topic

服务器:
    ssh root@10.234.81.212
    systemctl stop helper
    cd /opt/helper/bot && python -m scripts.backfill_spec_topic
    systemctl start helper
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import delete, select, update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill_spec_topic")


def main() -> int:
    from helper.config import get_settings
    from helper.ingest.sink import _decision_embedding
    from helper.specgen.cluster import assign_topic, scan_topics_for_draft, topic_keys
    from helper.storage import session
    from helper.storage.db import init_engine
    from helper.storage.models import L1Item, SpecCandidate, SpecTopic

    import json

    settings = get_settings()
    db_path = settings.helper_data_dir / "helper.db"
    if not db_path.exists():
        legacy = settings.helper_data_dir / "helper.sqlite"
        if legacy.exists():
            db_path = legacy
    log.info("init engine: %s", db_path)
    init_engine(db_path)

    # ── 1. 删除 pending SpecCandidate ───────────────────────────────────────
    with session() as s:
        deleted = s.execute(
            delete(SpecCandidate)
            .where(SpecCandidate.review_status == "pending")
            .where(SpecCandidate.promoted_at.is_(None))
        )
        n_pending_deleted = deleted.rowcount or 0
    log.info("step 1: deleted %d pending SpecCandidate", n_pending_deleted)

    # ── 2. 回填 decision embedding ─────────────────────────────────────────
    with session() as s:
        rows = s.execute(
            select(L1Item.raw_id, L1Item.idx, L1Item.payload_json)
            .where(L1Item.type == "decision")
        ).all()
    n_decision = len(rows)
    n_filled = 0
    n_emb_skipped = 0
    n_emb_failed = 0
    for raw_id, idx, payload_json in rows:
        with session() as s:
            it = s.execute(
                select(L1Item).where(L1Item.raw_id == raw_id, L1Item.idx == idx)
            ).scalar_one_or_none()
            if it is None:
                continue
            if it.embedding and len(it.embedding) > 0:
                n_emb_skipped += 1
                continue
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            n_emb_failed += 1
            continue
        emb = _decision_embedding(payload)
        if not emb:
            n_emb_failed += 1
            continue
        with session() as s:
            it = s.execute(
                select(L1Item).where(L1Item.raw_id == raw_id, L1Item.idx == idx)
            ).scalar_one_or_none()
            if it is None:
                continue
            it.embedding = emb
        n_filled += 1
        if n_filled % 20 == 0:
            log.info("step 2: embedded %d / %d decisions", n_filled, n_decision)
    log.info(
        "step 2 done: decisions=%d skipped=%d filled=%d failed=%d",
        n_decision, n_emb_skipped, n_filled, n_emb_failed,
    )

    # ── 3. 清空 topic 表 + 重置 L1Item.topic_id ───────────────────────────
    with session() as s:
        s.execute(delete(SpecTopic))
        s.execute(update(L1Item).values(topic_id=None))
    log.info("step 3: cleared SpecTopic + reset L1Item.topic_id")

    # ── 4. 重建 SpecTopic 簇 ──────────────────────────────────────────────
    with session() as s:
        ordered = s.execute(
            select(L1Item.raw_id, L1Item.idx)
            .where(L1Item.type == "decision")
            .order_by(L1Item.created_at, L1Item.raw_id, L1Item.idx)
        ).all()
    n_assigned = 0
    n_assign_failed = 0
    for raw_id, idx in ordered:
        try:
            tid = assign_topic(raw_id, idx)
            if tid is not None:
                n_assigned += 1
        except Exception:  # noqa: BLE001
            log.exception("assign_topic failed raw=%s idx=%s", raw_id, idx)
            n_assign_failed += 1
        if n_assigned and n_assigned % 50 == 0:
            log.info("step 4: assigned %d / %d", n_assigned, len(ordered))

    with session() as s:
        n_topics = s.execute(select(SpecTopic.id)).scalars().all()
    log.info(
        "step 4 done: assigned=%d failed=%d → %d topics",
        n_assigned, n_assign_failed, len(n_topics),
    )

    # ── 5. 干跑 scan, 不真 draft ────────────────────────────────────────
    due = scan_topics_for_draft()
    log.info("step 5: scan_topics_for_draft → %d topic(s) due", len(due))
    for tid in due:
        keys = topic_keys(tid)
        log.info("  topic#%d: %d decisions, sample raw=%s", tid, len(keys), keys[:3])
    log.info("backfill done — daily 03:00 spec_topic_scan 起来后才会真 draft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
