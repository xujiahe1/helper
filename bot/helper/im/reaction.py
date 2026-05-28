"""Wave im.msg.reaction.{created,deleted}_v1 — 用户对 bot 回复贴 emoji 表情。

与 feedback(👍/👎 按钮)是两个独立事件:
  - feedback action_type ∈ {like, dislike, cancel_like, cancel_dislike}
  - reaction reaction_type 是任意 emoji_type 字符串(thumbsup / fire / heart / ...)

复用 ReactionLog 表,action_type 列写成 "reaction:<emoji>" / "reaction_deleted:<emoji>"
作前缀区分,避免新增表。同 (operator_id, msg_id) 仍用覆盖更新。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import AskAnswer, ReactionLog

log = logging.getLogger(__name__)


def is_reaction_event(event_type: str) -> bool:
    return event_type.startswith("im.msg.reaction.")


def handle(event_type: str, payload: dict[str, Any]) -> None:
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return
    operator = event.get("operator") or {}
    if not isinstance(operator, dict):
        return
    operator_id = operator.get("id") or ""
    operator_id_type = operator.get("id_type") or "union_id"
    operator_user_id = operator.get("user_id") or ""

    msg_id = event.get("msg_id") or ""
    reaction_type = event.get("reaction_type") or {}
    emoji = ""
    if isinstance(reaction_type, dict):
        emoji = reaction_type.get("emoji_type") or ""
    elif isinstance(reaction_type, str):
        emoji = reaction_type

    if not operator_id or not msg_id:
        log.warning("reaction event missing operator/msg_id: %s", event)
        return

    prefix = "reaction_deleted" if event_type.endswith(".deleted_v1") else "reaction"
    action_type = f"{prefix}:{emoji}" if emoji else prefix

    related_ask_id = None
    with session() as s:
        ask_row = s.execute(
            select(AskAnswer).where(AskAnswer.wave_msg_id == msg_id)
        ).scalar_one_or_none()
        if ask_row is not None:
            related_ask_id = ask_row.id

        existing = s.get(ReactionLog, (operator_id, msg_id))
        if existing is None:
            s.add(
                ReactionLog(
                    operator_id=operator_id,
                    msg_id=msg_id,
                    operator_id_type=operator_id_type,
                    operator_user_id=operator_user_id,
                    action_type=action_type,
                    related_ask_id=related_ask_id,
                    action_time=datetime.now(timezone.utc),
                )
            )
        else:
            existing.action_type = action_type
            existing.action_time = datetime.now(timezone.utc)
            if related_ask_id and not existing.related_ask_id:
                existing.related_ask_id = related_ask_id
        s.commit()
