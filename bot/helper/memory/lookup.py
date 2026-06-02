"""ask 拼接侧 — 把命中的 directive 转成 SYSTEM_PROMPT 用户偏好段。"""

from __future__ import annotations

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import Memory


def directives_for_ask(
    *,
    entity_refs: list[str] | None = None,
    directive_ids: list[int] | None = None,
) -> str:
    """返回拼进 SYSTEM_PROMPT 末尾的"## 用户偏好"段。

    召回规则(任一命中即拼):
    - scope=global 的 directive 一律拼
    - scope=entity 且 entity_refs 命中该 scope_ref → 拼(实体名命中路径)
    - 不论 scope, m.id 在 directive_ids 里 → 拼(directive 文本本身被 fts/vector 召回的路径)

    第三条是为了解决"题面里没有 entity 字面词,但语义上该套这条 directive"的场景。
    例如 memory#2(scope=entity:tachi)的 directive 文本明确写"涉及 iam 网关 / app_id"
    类问题路由 tachi, 题面"看 iam 网关接入文档"在 fts 里和 directive 文本共词命中,
    走 directive_ids 路径强行拼上, 不再依赖题面里出现"tachi"二字。
    """
    refs = set(entity_refs or [])
    ids = set(directive_ids or [])
    with session() as s:
        rows = s.execute(
            select(Memory).where(Memory.superseded_at.is_(None)).order_by(Memory.id)
        ).scalars().all()

    lines: list[str] = []
    for m in rows:
        hit_global = m.scope_type == "global"
        hit_entity = m.scope_type == "entity" and m.scope_ref in refs
        hit_id = m.id in ids
        if not (hit_global or hit_entity or hit_id):
            continue
        if m.scope_type == "entity" and m.scope_ref:
            lines.append(f"- 涉及『{m.scope_ref}』时:{m.directive}")
        else:
            lines.append(f"- {m.directive}")

    if not lines:
        return ""
    return "## 用户偏好\n" + "\n".join(lines)
