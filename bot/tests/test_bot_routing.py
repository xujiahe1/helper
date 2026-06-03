"""bot_routing — dispatch / 入站关联 / 超时清理.

覆盖:
- dispatch_route: 私聊外部 bot, DB 落 PendingRouting
- handle_bot_reply: 群聊场景找最近未消费 routing → 在群里回贴 + @ 原提问人
- handle_bot_reply: 私聊场景在私聊回(不 @ 自己)
- handle_bot_reply: 没有匹配 routing 时丢弃, 不发任何消息
- expire_old_routings: 5min 过期 → 标 expired + 通知用户
- consumed_at 已设的 routing 不再被关联
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from helper.storage.models import _utcnow


@pytest.fixture
def wave_action_log(monkeypatch: pytest.MonkeyPatch):
    """收集 send_message / reply_message / update_card_active 调用 (msg_id 字段对齐 Wave 真实返回)."""
    log: list[dict] = []

    def _fake_send(receiver_id: str, **kwargs: Any) -> dict:
        log.append({"kind": "send", "receiver_id": receiver_id, **kwargs})
        return {"data": {"msg_id": "om_fake_send"}, "retcode": 0}

    def _fake_reply(msg_id: str, **kwargs: Any) -> dict:
        log.append({"kind": "reply", "reply_to": msg_id, **kwargs})
        return {"data": {"msg_id": "om_fake_reply"}, "retcode": 0}

    def _fake_card_update(msg_id: str, **kwargs: Any) -> dict:
        log.append({"kind": "card_update", "msg_id": msg_id, **kwargs})
        return {"retcode": 0}

    monkeypatch.setattr("helper.im.wave_client.send_message", _fake_send)
    monkeypatch.setattr("helper.im.wave_client.reply_message", _fake_reply)
    monkeypatch.setattr("helper.im.wave_client.update_card_active", _fake_card_update)
    return log


def test_dispatch_route_sends_rich_text_at_target_and_writes_pending(
    db, settings, wave_action_log,
):
    from helper.im.bot_routing import dispatch_route
    from helper.storage import session
    from helper.storage.models import PendingRouting

    ok = dispatch_route(
        target_app_id="cli_tachi",
        via_label="tachi",
        forwarded_text="查下 app_id: cli_xxx 对应的 agent",
        original_raw_id=42,
        original_chat_id="oc_group",
        original_wave_msg_id="om_user_question",
        original_asker_domain="alice",
        tracker=None,
    )
    assert ok is True

    sends = [e for e in wave_action_log if e["kind"] == "send"]
    assert len(sends) == 1
    s = sends[0]
    assert s["receiver_id"] == "cli_tachi"
    assert s["receiver_id_type"] == "app_id"
    assert s["msg_type"] == "rich_text"
    # rich_text content 第一个 item 是 at 节点, 第二个是 text
    items = s["content"]["tags"][0]["items"]
    assert items[0]["type"] == "at"
    assert items[0]["content"]["id"] == "cli_tachi"
    assert items[0]["content"]["id_type"] == "app_id"
    assert items[1]["type"] == "text"
    assert "查下 app_id" in items[1]["content"]["text"]

    with session() as sess:
        rows = sess.query(PendingRouting).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.target_app_id == "cli_tachi"
    assert r.via_label == "tachi"
    assert r.original_raw_id == 42
    assert r.original_chat_id == "oc_group"
    assert r.original_asker_domain == "alice"
    assert r.consumed_at is None
    assert r.expired_at is None


def test_dispatch_route_send_failure_returns_false_no_db_row(
    db, settings, monkeypatch,
):
    from helper.im import wave_client
    from helper.im.bot_routing import dispatch_route
    from helper.storage import session
    from helper.storage.models import PendingRouting

    def _boom(*a, **kw):
        raise wave_client.WaveAPIError(retcode=1, message="boom")

    monkeypatch.setattr("helper.im.wave_client.send_message", _boom)

    ok = dispatch_route(
        target_app_id="cli_tachi", via_label="tachi", forwarded_text="x",
        original_raw_id=1, original_chat_id="", original_wave_msg_id="",
        original_asker_domain="alice", tracker=None,
    )
    assert ok is False
    with session() as sess:
        assert sess.query(PendingRouting).count() == 0


def test_handle_bot_reply_group_replies_with_prefix_and_forwards(
    db, settings, wave_action_log, llm_stub,
):
    """群聊: 1 条 reply 前缀("@asker 已咨询 @via:") + 1 张 markdown card(LLM 转述结果)。"""
    import json as _json

    from helper.im.bot_routing import dispatch_route, handle_bot_reply
    from helper.storage import session
    from helper.storage.models import PendingRouting

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi",
        forwarded_text="查下 app_id: cli_xxx 对应的 agent",
        original_raw_id=10, original_chat_id="oc_group",
        original_wave_msg_id="om_user_q", original_asker_domain="alice",
        tracker=None,
    )

    llm_stub.set("restate_bot_reply", "## 查询结果\n- agent_name: HYG谷多曼\n- owner: zhishuang.li")

    reply_payload = {
        "event": {
            "sender": {"id": "cli_tachi", "id_type": "app_id"},
            "message": {
                "msg_type": "text",
                "content": '{"text":"agent_name: HYG谷多曼, owner: zhishuang.li"}',
            },
        },
    }
    ok = handle_bot_reply(reply_payload, sender_app_id="cli_tachi")
    assert ok is True

    # a) 前缀走 reply_message(quote 用户原问题)
    reply_calls = [e for e in wave_action_log if e["kind"] == "reply"]
    assert len(reply_calls) == 1
    prefix_payload = _json.loads(reply_calls[0]["content"])
    assert prefix_payload["text"] == "@alice 已咨询 @tachi:"
    assert reply_calls[0]["msg_type"] == "text"

    # b) 转述后用 markdown card 发到原会话
    sends = [e for e in wave_action_log if e["kind"] == "send" and e.get("receiver_id") == "oc_group"]
    assert len(sends) == 1
    assert sends[0]["msg_type"] == "card"
    rebuilt = _json.loads(sends[0]["content"])
    assert rebuilt["card"]["tag"] == "flow"
    el = rebuilt["card"]["elements"]
    assert len(el) == 1 and el[0]["tag"] == "markdown"
    assert "HYG谷多曼" in el[0]["text"]
    assert "zhishuang.li" in el[0]["text"]

    # consumed_at 已标
    with session() as sess:
        r = sess.query(PendingRouting).first()
    assert r.consumed_at is not None


def test_handle_bot_reply_dm_replies_in_dm_no_at(
    db, settings, wave_action_log, llm_stub,
):
    """私聊: 2 条 send 给 asker(无 chat_id 不走 reply): 前缀 "已咨询 @via:" + markdown card。"""
    import json as _json

    from helper.im.bot_routing import dispatch_route, handle_bot_reply

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi",
        forwarded_text="hello", original_raw_id=11,
        original_chat_id="",  # 私聊场景
        original_wave_msg_id="om_dm",
        original_asker_domain="alice",
        tracker=None,
    )
    llm_stub.set("restate_bot_reply", "**重写后的 answer body**")

    reply_payload = {
        "event": {
            "sender": {"id": "cli_tachi", "id_type": "app_id"},
            "message": {"msg_type": "text", "content": '{"text":"answer body"}'},
        },
    }
    handle_bot_reply(reply_payload, sender_app_id="cli_tachi")

    sends_to_alice = [
        e for e in wave_action_log
        if e["kind"] == "send" and e.get("receiver_id") == "alice"
    ]
    assert len(sends_to_alice) == 2

    # 第 1 条: 前缀, 私聊不 @ 自己
    prefix = _json.loads(sends_to_alice[0]["content"])
    assert prefix["text"] == "已咨询 @tachi:"
    assert "@alice" not in prefix["text"]
    assert sends_to_alice[0]["msg_type"] == "text"

    # 第 2 条: LLM 转述后的 markdown card
    assert sends_to_alice[1]["msg_type"] == "card"
    rebuilt = _json.loads(sends_to_alice[1]["content"])
    assert rebuilt["card"]["elements"][0]["text"] == "**重写后的 answer body**"


def test_handle_bot_reply_no_pending_drops(db, settings, wave_action_log):
    from helper.im.bot_routing import handle_bot_reply

    payload = {
        "event": {
            "sender": {"id": "cli_unknown_bot", "id_type": "app_id"},
            "message": {"msg_type": "text", "content": '{"text":"orphan"}'},
        },
    }
    ok = handle_bot_reply(payload, sender_app_id="cli_unknown_bot")
    assert ok is False
    # 没有任何外发
    assert wave_action_log == []


def test_handle_bot_reply_forwards_card_via_llm_restate(
    db, settings, wave_action_log, llm_stub,
):
    """tachi 回 card(i18n_text 扁平字符串)时,helper 抽原文 → LLM 转述 → markdown card。
    验证: LLM 拿到的是抽出的原文; 卡片正文是 LLM 输出。
    """
    from helper.im.bot_routing import dispatch_route, handle_bot_reply
    import json as _json

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi", forwarded_text="x",
        original_raw_id=20, original_chat_id="oc_group",
        original_wave_msg_id="om_q", original_asker_domain="bob",
        tracker=None,
    )
    card_content_str = _json.dumps({
        "i18n_text": {"zh-cn": "查询到了 👇 agent_name: HYG", "en": "got it"}
    })

    captured: dict = {}
    def _restate(system, user, **kw):
        captured["user"] = user
        return "## 结果\n- agent_name: HYG"
    llm_stub.set("restate_bot_reply", _restate)

    payload = {
        "event": {
            "sender": {"id": "cli_tachi", "id_type": "app_id"},
            "message": {"msg_type": "card", "content": card_content_str},
        },
    }
    handle_bot_reply(payload, sender_app_id="cli_tachi")

    # LLM 拿到的 user 内容里包含 helper 从 i18n_text 抽出来的原文
    assert "查询到了" in captured["user"]
    assert "agent_name: HYG" in captured["user"]
    assert "@tachi" in captured["user"]

    # 前缀
    reply_calls = [e for e in wave_action_log if e["kind"] == "reply"]
    assert len(reply_calls) == 1
    assert _json.loads(reply_calls[0]["content"])["text"] == "@bob 已咨询 @tachi:"

    # markdown card 正文 = LLM 输出
    sends = [e for e in wave_action_log if e["kind"] == "send" and e.get("receiver_id") == "oc_group"]
    assert len(sends) == 1
    assert sends[0]["msg_type"] == "card"
    rebuilt = _json.loads(sends[0]["content"])
    assert rebuilt["card"]["tag"] == "flow"
    el = rebuilt["card"]["elements"]
    assert len(el) == 1 and el[0]["tag"] == "markdown"
    assert el[0]["text"] == "## 结果\n- agent_name: HYG"


def test_handle_bot_reply_llm_failure_falls_back_to_extracted_text(
    db, settings, wave_action_log, llm_stub,
):
    """LLM 转述失败 → 兜底用抽出的扁平文本直发(text 消息)。"""
    from helper.im.bot_routing import dispatch_route, handle_bot_reply
    import json as _json

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi", forwarded_text="x",
        original_raw_id=22, original_chat_id="oc_group",
        original_wave_msg_id="om_q", original_asker_domain="bob",
        tracker=None,
    )
    card_content_str = _json.dumps({
        "i18n_text": {"zh-cn": "扁平的回复"}
    })
    def _boom(**kw):
        raise RuntimeError("athenai down")
    llm_stub.set("restate_bot_reply", _boom)

    payload = {
        "event": {
            "sender": {"id": "cli_tachi", "id_type": "app_id"},
            "message": {"msg_type": "card", "content": card_content_str},
        },
    }
    handle_bot_reply(payload, sender_app_id="cli_tachi")

    sends = [e for e in wave_action_log if e["kind"] == "send" and e.get("receiver_id") == "oc_group"]
    assert len(sends) == 1
    # 兜底走 text 直发
    assert sends[0]["msg_type"] == "text"
    assert _json.loads(sends[0]["content"])["text"] == "扁平的回复"


def test_handle_bot_reply_consumed_routing_not_picked_again(
    db, settings, wave_action_log, llm_stub,
):
    from helper.im.bot_routing import dispatch_route, handle_bot_reply

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi", forwarded_text="x",
        original_raw_id=30, original_chat_id="oc_g",
        original_wave_msg_id="om_q", original_asker_domain="alice",
        tracker=None,
    )
    llm_stub.set("restate_bot_reply", "first")
    payload = {
        "event": {
            "sender": {"id": "cli_tachi", "id_type": "app_id"},
            "message": {"msg_type": "text", "content": '{"text":"first"}'},
        },
    }
    assert handle_bot_reply(payload, sender_app_id="cli_tachi") is True
    # 第二次同 sender 进来 → 没未消费的 routing 了
    assert handle_bot_reply(payload, sender_app_id="cli_tachi") is False


def test_expire_old_routings_marks_expired_and_notifies(
    db, settings, wave_action_log,
):
    from helper.im.bot_routing import dispatch_route, expire_old_routings
    from helper.storage import session
    from helper.storage.models import PendingRouting

    dispatch_route(
        target_app_id="cli_tachi", via_label="tachi", forwarded_text="x",
        original_raw_id=50, original_chat_id="oc_g",
        original_wave_msg_id="om_q", original_asker_domain="alice",
        tracker=None,
    )
    # 把 created_at 倒回 6 分钟前
    with session() as sess:
        r = sess.query(PendingRouting).first()
        r.created_at = _utcnow() - timedelta(minutes=6)

    n = expire_old_routings()
    assert n == 1

    # 用户收到超时提示
    reply_calls = [e for e in wave_action_log if e["kind"] == "reply"]
    assert len(reply_calls) == 1
    text = reply_calls[0]["content"]["text"]
    assert "@alice" in text
    assert "@tachi" in text
    assert "5 分钟" in text or "没回" in text

    with session() as sess:
        r = sess.query(PendingRouting).first()
    assert r.expired_at is not None
    assert r.consumed_at is None


def test_expire_does_not_touch_consumed_or_fresh(db, settings, wave_action_log, llm_stub):
    from helper.im.bot_routing import dispatch_route, expire_old_routings, handle_bot_reply
    from helper.storage import session
    from helper.storage.models import PendingRouting

    # routing#1: fresh (created just now), 不该 expire
    dispatch_route(
        target_app_id="cli_a", via_label="a", forwarded_text="x",
        original_raw_id=1, original_chat_id="",
        original_wave_msg_id="", original_asker_domain="u1", tracker=None,
    )

    # routing#2: 已消费, 不该 re-expire
    dispatch_route(
        target_app_id="cli_b", via_label="b", forwarded_text="y",
        original_raw_id=2, original_chat_id="",
        original_wave_msg_id="", original_asker_domain="u2", tracker=None,
    )
    llm_stub.set("restate_bot_reply", "ok")
    handle_bot_reply(
        {"event": {"sender": {"id": "cli_b", "id_type": "app_id"},
                   "message": {"msg_type": "text", "content": '{"text":"ok"}'}}},
        sender_app_id="cli_b",
    )

    n = expire_old_routings()
    assert n == 0

    with session() as sess:
        rows = sess.query(PendingRouting).order_by(PendingRouting.id).all()
    assert rows[0].expired_at is None and rows[0].consumed_at is None
    assert rows[1].consumed_at is not None and rows[1].expired_at is None
