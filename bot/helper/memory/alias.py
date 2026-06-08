"""同义实体表读写 — 负责把 "小猫老师" / "周婷" 这种同一对象的不同名字归到一个主名。

设计纪律:
- 落库 / 查询 Memory.scope_ref 之前都过 resolve_alias(name)。 没声明同义时
  fallback 原值, 不报错。
- "owner 在 chat 说 X 就是 Y" 由 memory_extract 抽成 alias 声明 (type=alias)
  落到这里, **不**进 Memory 表。
- "向量相似度高 + owner 在周报采纳" 由 conflict resolve 调 add_alias(source='auto')
  回写。
- mark_not_alias 把两个名字标 source='reverted', canonical=name 自身, 防止
  auto 路径反复触发 (owner 已经判过它们不是同一个)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import EntityAlias

log = logging.getLogger(__name__)


def resolve_alias(name: str) -> str:
    """name → canonical 主名。 没记录就返回原值, fallback 不报错。"""
    if not name:
        return name
    with session() as s:
        row = s.execute(
            select(EntityAlias).where(EntityAlias.name == name)
        ).scalar_one_or_none()
    if row is None:
        return name
    if row.source == "reverted":
        # owner 显式判过"不是同义", canonical 应等于 name 自身
        return name
    return row.canonical or name


def add_alias(name: str, canonical: str, *, source: str = "manual") -> None:
    """登记 name → canonical。 同时确保 canonical → canonical 自映射存在,
    方便 resolve 一律走表。 重复 name 不报错, 沿用旧记录(不会被 auto 后写覆盖
    manual)。"""
    if not name or not canonical:
        return
    if source not in ("manual", "auto", "reverted"):
        source = "manual"
    now = datetime.now(timezone.utc)
    with session() as s:
        existing = s.execute(
            select(EntityAlias).where(EntityAlias.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            # manual 优先级最高, auto 不能覆盖 manual; reverted 同理
            if existing.source == "manual" and source != "manual":
                return
            existing.canonical = canonical
            existing.source = source
        else:
            s.add(EntityAlias(
                name=name, canonical=canonical, source=source, created_at=now,
            ))
        # 主名自映射 (canonical → canonical), 仅当不存在时插入
        if name != canonical:
            self_row = s.execute(
                select(EntityAlias).where(EntityAlias.name == canonical)
            ).scalar_one_or_none()
            if self_row is None:
                s.add(EntityAlias(
                    name=canonical, canonical=canonical, source=source,
                    created_at=now,
                ))
        s.commit()
    log.info("entity_alias add %s -> %s (source=%s)", name, canonical, source)


def mark_not_alias(name_a: str, name_b: str) -> None:
    """owner 在周报上选了 "保留"(否决疑似同义) → 标两个名字 reverted, 防 auto 再触发。

    canonical 设回各自原值, 让 resolve_alias 返回原值 (虽然 source=reverted 已经
    有 fallback 逻辑, 这里保持数据语义清晰)。"""
    for n in (name_a, name_b):
        if not n:
            continue
        with session() as s:
            existing = s.execute(
                select(EntityAlias).where(EntityAlias.name == n)
            ).scalar_one_or_none()
            if existing is None:
                s.add(EntityAlias(
                    name=n, canonical=n, source="reverted",
                    created_at=datetime.now(timezone.utc),
                ))
            else:
                existing.source = "reverted"
                existing.canonical = n
            s.commit()
    log.info("entity_alias mark_not_alias %s vs %s", name_a, name_b)
