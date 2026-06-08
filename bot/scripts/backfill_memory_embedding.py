"""为存量 alive memory 回填 embedding (改动 2 上线前必跑)。

阻塞上线: backfill 不跑 → 存量 memory.embedding 全空 → 跨 scope 语义 fallback
永远 miss (精确路径仍正常工作, 不出错, 但语义同义检测对老数据失效)。

幂等: 跳过 embedding 已非空的行。 重复跑安全。

跑法:
    cd bot/
    .venv/bin/python -m scripts.backfill_memory_embedding

服务器:
    ssh root@10.234.81.212
    cd /opt/helper/bot && python -m scripts.backfill_memory_embedding
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill_memory_embedding")


def main() -> int:
    from helper.config import get_settings
    from helper.memory.extract import _compute_embedding
    from helper.storage import session
    from helper.storage.db import init_engine
    from helper.storage.models import Memory

    settings = get_settings()
    db_path = settings.helper_data_dir / "helper.db"
    if not db_path.exists():
        # 兼容旧路径
        legacy = settings.helper_data_dir / "helper.sqlite"
        if legacy.exists():
            db_path = legacy
    log.info("init engine: %s", db_path)
    init_engine(db_path)

    n_total = 0
    n_skipped = 0
    n_filled = 0
    n_failed = 0

    with session() as s:
        rows = s.execute(
            select(Memory.id, Memory.directive, Memory.embedding)
            .where(Memory.superseded_at.is_(None))
            .order_by(Memory.id)
        ).all()

    n_total = len(rows)
    log.info("alive memory rows: %d", n_total)

    for mem_id, directive, blob in rows:
        if blob and len(blob) > 0:
            n_skipped += 1
            continue
        emb = _compute_embedding(directive or "")
        if not emb:
            log.warning("memory#%d compute_embedding empty (skip)", mem_id)
            n_failed += 1
            continue
        with session() as s:
            mem = s.get(Memory, mem_id)
            if mem is None:
                continue
            mem.embedding = emb
        n_filled += 1
        if n_filled % 10 == 0:
            log.info("filled %d / %d", n_filled, n_total)

    log.info(
        "done — total=%d skipped=%d filled=%d failed=%d",
        n_total, n_skipped, n_filled, n_failed,
    )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
