"""Wave im.msg.feedback.action_v1 事件 — 用户对 bot 回复点 👍/👎。

按 (operator_id, msg_id) 复合主键覆盖更新,不做加减统计。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import AskAnswer, ReactionLog

log = logging.getLogger(__name__)


def is_feedback_event(event_type: str) -> bool:
    return event_type.startswith("im.msg.feedback.action")


def handle(payload: dict[str, Any]) -> None:
    """处理一个 feedback 事件,落 reaction_log。"""
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
    action_type = event.get("action_type") or ""

    if not operator_id or not msg_id:
        log.warning("feedback event missing operator/msg_id: %s", event)
        return

    # 反查这条 bot msg 对应哪次 Ask
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
