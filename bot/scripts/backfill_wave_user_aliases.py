"""一次性把现有所有出现过的域账号通过 Wave OpenAPI 拉中文名落 entity_alias。

覆盖来源:
  - ask_answers.asker_domain (有人 @bot 提过问)
  - raw_inputs.author_domain (有人在群里发过消息被 ingest)

幂等: 已有 manual 记录的不覆盖 (alias.add_alias 内部判优先级);
      已有 auto 记录的会被覆盖成最新值 (Wave 那边改名了应该跟随)。

跑法:
    cd bot/
    .venv/bin/python -m scripts.backfill_wave_user_aliases

服务器:
    ssh root@10.234.81.212
    sudo -u helper bash -c '
        set -a; . /etc/helper/helper.env; . /etc/helper/wave.env; set +a;
        cd /opt/helper/bot && .venv/bin/python -m scripts.backfill_wave_user_aliases
    '
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill_wave_user_aliases")


def main() -> int:
    from helper.config import get_settings
    from helper.im.wave_user import get_user_chinese_names
    from helper.memory.alias import add_alias
    from helper.storage import session
    from helper.storage.db import init_engine
    from helper.storage.models import AskAnswer, RawInput

    settings = get_settings()
    db_path = settings.helper_data_dir / "helper.db"
    if not db_path.exists():
        legacy = settings.helper_data_dir / "helper.sqlite"
        if legacy.exists():
            db_path = legacy
    log.info("init engine: %s", db_path)
    init_engine(db_path)

    with session() as s:
        askers = s.execute(
            select(AskAnswer.asker_domain).where(AskAnswer.asker_domain != "").distinct()
        ).scalars().all()
        authors = s.execute(
            select(RawInput.author_domain).where(RawInput.author_domain != "").distinct()
        ).scalars().all()
    domains = sorted(set(askers) | set(authors))
    # 排除明显非真实用户的占位
    domains = [d for d in domains if d not in ("", "admin", "smoke", "replay", "system")]
    log.info("domains to resolve: %d (%s...)", len(domains), domains[:5])

    if not domains:
        log.info("nothing to do")
        return 0

    # Wave API 单次最多 200, 我们这里量级远低于, 不切批
    name_map = get_user_chinese_names(domains)
    log.info("wave returned %d/%d names", len(name_map), len(domains))

    n_added = 0
    n_missing = 0
    for d in domains:
        canon = name_map.get(d)
        if not canon:
            n_missing += 1
            log.warning("no chinese name for domain=%s (skipped)", d)
            continue
        add_alias(d, canon, source="auto")
        log.info("alias %s -> %s", d, canon)
        n_added += 1

    log.info("backfill done: added=%d missing=%d total=%d", n_added, n_missing, len(domains))
    return 0


if __name__ == "__main__":
    sys.exit(main())
