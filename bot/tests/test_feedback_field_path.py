"""feedback handler 字段路径回归 — action_type 必须从 event.feedback_info.action_type 抽。

之前实现写成 event.action_type(扁平),Wave 实际下发是嵌套 feedback_info.action_type,
导致 ReactionLog 永远拿到空字符串。
"""

from __future__ import annotations


def test_feedback_action_type_from_nested_feedback_info(db, settings):
    from helper.im import feedback as fb
    from helper.storage import session
    from helper.storage.models import ReactionLog

    payload = {
        "schema": "1.0",
        "header": {"event_type": "im.msg.feedback.action_v1", "event_id": "e1"},
        "event": {
            "msg_id": "om_abc",
            "operator": {"id": "ou_x", "id_type": "union_id", "user_id": "alice"},
            "feedback_info": {"action_type": "like"},
        },
    }
    fb.handle(payload)

    with session() as s:
        row = s.get(ReactionLog, ("ou_x", "om_abc"))
    assert row is not None
    assert row.action_type == "like"
    assert row.operator_user_id == "alice"


def test_feedback_dislike_overwrites_like(db, settings):
    """同 (operator, msg) 第二次反馈应覆盖前一次。"""
    from helper.im import feedback as fb
    from helper.storage import session
    from helper.storage.models import ReactionLog

    base = {
        "schema": "1.0",
        "header": {"event_type": "im.msg.feedback.action_v1"},
        "event": {
            "msg_id": "om_x",
            "operator": {"id": "ou_y", "id_type": "union_id", "user_id": "bob"},
        },
    }
    fb.handle({**base, "event": {**base["event"], "feedback_info": {"action_type": "like"}}})
    fb.handle({**base, "event": {**base["event"], "feedback_info": {"action_type": "dislike"}}})

    with session() as s:
        row = s.get(ReactionLog, ("ou_y", "om_x"))
    assert row.action_type == "dislike"


def test_feedback_missing_feedback_info_does_not_crash(db, settings):
    from helper.im import feedback as fb

    fb.handle({
        "header": {"event_type": "im.msg.feedback.action_v1"},
        "event": {
            "msg_id": "om_z",
            "operator": {"id": "ou_z", "id_type": "union_id"},
        },
    })
    # 不抛异常即可,action_type 会是空串,这种 row 不应该入库还是入库都行;关键是不 500
