"""把 open 冲突推到 IM 群,@ 相关专家。

调用约定:
- 在某个群配置 expert_user_ids(域账号 list)
- run_notifications(chat_id, experts, *, max_items=5) 推一条文本消息

简单实现 — Wave Lark 协议群消息加 @ 走 rich_text:
content = {
    "tags": [{"items": [
        {"type": "text", "content": {"text": "..."}},
        {"type": "user", "content": {"user_id": "<union_id 或 user_id>"}}
    ]}]
}
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from helper.im import wave_client
from helper.im.wave_client import WaveAPIError
from helper.storage import session
from helper.storage.models import ConflictLog

log = logging.getLogger(__name__)


def _build_rich_text(experts: list[str], conflicts: list[ConflictLog]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    items.append({"type": "text", "content": {"text": "🛎 待裁决冲突:\n\n"}})
    for c in conflicts:
        items.append({
            "type": "text",
            "content": {
                "text": f"  • #{c.id} vs spec={c.spec_slug} [{c.severity}]\n    {c.summary[:120]}\n\n"
            },
        })
    items.append({"type": "text", "content": {"text": "请 "}})
    for u in experts:
        items.append({"type": "user", "content": {"user_id": u}})
        items.append({"type": "text", "content": {"text": " "}})
    items.append({"type": "text", "content": {"text": "看看处理。"}})
    return {"tags": [{"items": items}]}


def run_notifications(
    chat_id: str,
    experts: list[str],
    *,
    max_items: int = 5,
) -> int:
    """推一条 @ 专家的消息。返推送条数(0 = 没 open 冲突或失败)。"""
    if not chat_id or not experts:
        return 0
    with session() as s:
        rows = s.execute(
            select(ConflictLog)
            .where(ConflictLog.resolution == "open")
            .order_by(ConflictLog.created_at.desc())
            .limit(max_items)
        ).scalars().all()
    if not rows:
        return 0
    content = _build_rich_text(experts, list(rows))
    try:
        wave_client.send_message(
            chat_id,
            msg_type="post",
            content=content,
            receiver_id_type="chat_id",
            send_type=1,
        )
    except WaveAPIError as e:
        log.warning("conflict notify chat=%s failed: %s", chat_id, e)
        return 0
    return len(rows)
