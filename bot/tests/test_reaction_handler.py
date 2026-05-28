"""im.msg.reaction.{created,deleted}_v1 — emoji 表情回复落 ReactionLog。

reaction 与 feedback 是两个独立事件,reaction 的 action_type 列以 reaction:<emoji> 前缀
区分,与 feedback 的 like/dislike 同表共存。
"""

from __future__ import annotations


def test_reaction_created_logs_with_emoji_prefix(db, settings):
    from helper.im import reaction as rx
    from helper.storage import session
    from helper.storage.models import ReactionLog

    payload = {
        "header": {"event_type": "im.msg.reaction.created_v1"},
        "event": {
            "msg_id": "om_r1",
            "operator": {"id": "ou_a", "id_type": "union_id", "user_id": "alice"},
            "reaction_type": {"emoji_type": "thumbsup"},
        },
    }
    rx.handle("im.msg.reaction.created_v1", payload)

    with session() as s:
        row = s.get(ReactionLog, ("ou_a", "om_r1"))
    assert row is not None
    assert row.action_type == "reaction:thumbsup"


def test_reaction_deleted_uses_deleted_prefix(db, settings):
    from helper.im import reaction as rx
    from helper.storage import session
    from helper.storage.models import ReactionLog

    payload = {
        "header": {"event_type": "im.msg.reaction.deleted_v1"},
        "event": {
            "msg_id": "om_r2",
            "operator": {"id": "ou_b", "id_type": "union_id"},
            "reaction_type": {"emoji_type": "fire"},
        },
    }
    rx.handle("im.msg.reaction.deleted_v1", payload)

    with session() as s:
        row = s.get(ReactionLog, ("ou_b", "om_r2"))
    assert row.action_type == "reaction_deleted:fire"


def test_is_reaction_event():
    from helper.im.reaction import is_reaction_event

    assert is_reaction_event("im.msg.reaction.created_v1")
    assert is_reaction_event("im.msg.reaction.deleted_v1")
    assert not is_reaction_event("im.msg.feedback.action_v1")
    assert not is_reaction_event("im.msg.direct.sent_v2")
