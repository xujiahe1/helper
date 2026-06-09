"""清理 L1 抽取吃 bot 自答 context 产生的脏 atom (一次性).

根因: ingest/sink.py 群聊 @bot 路径用 list_chat_history() 拉上下文喂 LLM,
而该函数早期没排除 source_type='im_wave_bot*', 加上 _persist_bot_reply
落库时 author_domain 写的是接收方域账号, LLM 完全识别不出哪行是 bot 自答,
把 bot 长回复(如自动写的微小说)也切成了 section 挂到主 raw 下。

清理范围: 群聊@bot 的 raw 中 "主消息 < 200字 且 atom 总体积 ≥ 主消息*5" 的,
强污染指标 — 这种比例只可能是从 context 吃了别处长内容。

每条:
  - vec.delete_l1_atoms_for_raw / fts.delete_l1_atoms_for_raw
  - DELETE FROM l1_items WHERE raw_id = ?
  - l1_results.error = 'purged:bot_context_pollution'
  - raw.processed = True 保持 (raw 本身不动, 只清抽出来的脏知识)

用法:
  python -m helper.scripts.purge_bot_context_pollution           # dry-run
  python -m helper.scripts.purge_bot_context_pollution --apply   # 真执行
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import delete, func, select

from helper.storage import session
from helper.storage import vector as vec
from helper.storage import fts
from helper.storage.models import L1Item, L1Result, RawInput

log = logging.getLogger("purge_bot_context_pollution")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MSG_LEN_THRESHOLD = 200
ATOM_LEN_RATIO = 5  # atom 总体积 ≥ msg_len * 5 视为污染


def find_polluted_raws() -> list[tuple[int, str, int, int, int]]:
    """返回 (raw_id, author_domain, msg_len, n_atoms, atoms_len)."""
    with session() as s:
        subq_atoms = (
            select(
                L1Item.raw_id.label("raw_id"),
                func.count().label("n_atoms"),
                func.sum(func.length(L1Item.payload_json)).label("atoms_len"),
            )
            .group_by(L1Item.raw_id)
            .subquery()
        )
        rows = s.execute(
            select(
                RawInput.id,
                RawInput.author_domain,
                func.length(RawInput.content_text),
                subq_atoms.c.n_atoms,
                subq_atoms.c.atoms_len,
            )
            .join(L1Result, L1Result.raw_id == RawInput.id)
            .join(subq_atoms, subq_atoms.c.raw_id == RawInput.id)
            .where(RawInput.chat_id != "")
            .where(RawInput.is_at_bot.is_(True))
            .where(L1Result.error == "")
            .where(func.length(RawInput.content_text) < MSG_LEN_THRESHOLD)
            .where(subq_atoms.c.atoms_len >= func.length(RawInput.content_text) * ATOM_LEN_RATIO)
            .order_by(subq_atoms.c.atoms_len.desc())
        ).all()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def purge_one(raw_id: int) -> None:
    with session() as s:
        try:
            vec.delete_l1_atoms_for_raw(s, raw_id)
        except Exception:  # noqa: BLE001
            log.exception("vec.delete_l1_atoms_for_raw failed raw_id=%s", raw_id)
        try:
            fts.delete_l1_atoms_for_raw(s, raw_id)
        except Exception:  # noqa: BLE001
            log.exception("fts.delete_l1_atoms_for_raw failed raw_id=%s", raw_id)
        s.execute(delete(L1Item).where(L1Item.raw_id == raw_id))
        result = s.get(L1Result, raw_id)
        if result is not None:
            result.error = "purged:bot_context_pollution"
            result.model = "manual_purge"
        s.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真执行,默认 dry-run")
    parser.add_argument("--db", default="", help="DB 文件路径 (默认走 settings)")
    args = parser.parse_args()

    from pathlib import Path
    from helper.config import get_settings
    from helper.storage.db import init_engine

    if args.db:
        db_path = Path(args.db)
    else:
        settings = get_settings()
        db_path = settings.helper_data_dir / "helper.db"
    log.info("init engine: %s", db_path)
    init_engine(db_path)

    candidates = find_polluted_raws()
    log.info("found %d polluted raws (msg_len<%d, atoms_len ≥ msg_len*%d):",
             len(candidates), MSG_LEN_THRESHOLD, ATOM_LEN_RATIO)
    log.info("%-6s %-15s %-10s %-9s %-10s", "raw_id", "author", "msg_len", "n_atoms", "atoms_len")
    for raw_id, author, msg_len, n_atoms, atoms_len in candidates:
        log.info("%-6d %-15s %-10d %-9d %-10d",
                 raw_id, author or "", msg_len, n_atoms, atoms_len)

    if not args.apply:
        log.info("\ndry-run only. re-run with --apply to actually purge.")
        return

    log.info("\napplying...")
    for raw_id, _author, _msg_len, _n, _len in candidates:
        purge_one(raw_id)
        log.info("purged raw_id=%d", raw_id)
    log.info("done. %d raws purged.", len(candidates))


if __name__ == "__main__":
    main()
