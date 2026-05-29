"""ask 拼接侧 — 把命中的 directive 转成 SYSTEM_PROMPT 用户偏好段。"""

from __future__ import annotations

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import Memory


def directives_for_ask(*, entity_refs: list[str] | None = None) -> str:
    """返回拼进 SYSTEM_PROMPT 末尾的"## 用户偏好"段。

    召回规则:
    - scope=global: 全部 alive directive
    - scope=entity: 仅当 entity_refs 里出现该 scope_ref 时才捞(避免无关偏好打扰)

    无任何命中 → 返空字符串(调用方判 falsy 跳过拼接)。
    """
    refs = set(entity_refs or [])
    with session() as s:
        rows = s.execute(
            select(Memory).where(Memory.superseded_at.is_(None)).order_by(Memory.id)
        ).scalars().all()

    lines: list[str] = []
    for m in rows:
        if m.scope_type == "global":
            lines.append(f"- {m.directive}")
        elif m.scope_type == "entity" and m.scope_ref in refs:
            lines.append(f"- 涉及『{m.scope_ref}』时:{m.directive}")

    if not lines:
        return ""
    return "## 用户偏好\n" + "\n".join(lines)
