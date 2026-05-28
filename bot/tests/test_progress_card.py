"""ThinkingTracker — start / finish / fail 三态行为。

不打 Wave 真接口,monkeypatch wave_client 的 send_message + update_card_active。
"""

from __future__ import annotations


def _patch_wave(monkeypatch):
    """打桩 send_message / update_card_active,返回它们各自的 calls 列表。"""
    from helper.im import progress_card

    sends: list[dict] = []
    updates: list[dict] = []

    def fake_send(receiver_id, *, msg_type, content, receiver_id_type, send_type=1, request_id=None):
        sends.append({
            "receiver_id": receiver_id,
            "msg_type": msg_type,
            "content": content,
            "receiver_id_type": receiver_id_type,
            "request_id": request_id,
        })
        return {"data": {"msg_id": "om_fake_card_001"}}

    def fake_update(msg_id, *, content, receiver_ids=None, receiver_id_type=None):
        updates.append({"msg_id": msg_id, "content": content})
        return {}

    monkeypatch.setattr(progress_card.wave_client, "send_message", fake_send)
    monkeypatch.setattr(progress_card.wave_client, "update_card_active", fake_update)
    return sends, updates


def test_start_sends_thinking_card(monkeypatch):
    from helper.im.progress_card import THINKING_TEXT, ThinkingTracker

    sends, updates = _patch_wave(monkeypatch)
    t = ThinkingTracker(receiver_id="oc_xxx", receiver_id_type="chat_id").start()

    assert t.msg_id == "om_fake_card_001"
    assert len(sends) == 1
    s = sends[0]
    assert s["msg_type"] == "card"
    assert s["receiver_id_type"] == "chat_id"
    # content 是 dict,卡片正文是"思考中"
    md = s["content"]["card"]["elements"][0]
    assert md["tag"] == "markdown"
    assert THINKING_TEXT in md["text"]
    assert updates == []


def test_finish_updates_card_inplace(monkeypatch):
    from helper.im.progress_card import ThinkingTracker

    sends, updates = _patch_wave(monkeypatch)
    t = ThinkingTracker(receiver_id="liang.xue", receiver_id_type="user_id").start()
    t.finish("✓ 已记录(3 条原子)")

    assert len(updates) == 1
    u = updates[0]
    assert u["msg_id"] == "om_fake_card_001"
    assert "已记录" in u["content"]["card"]["elements"][0]["text"]


def test_fail_updates_card_with_error_text(monkeypatch):
    from helper.im.progress_card import ERROR_TEXT, ThinkingTracker

    sends, updates = _patch_wave(monkeypatch)
    t = ThinkingTracker(receiver_id="liang.xue", receiver_id_type="user_id").start()
    t.fail()

    assert len(updates) == 1
    assert ERROR_TEXT in updates[0]["content"]["card"]["elements"][0]["text"]


def test_finish_then_fail_idempotent(monkeypatch):
    """同一 tracker 上 finish 后再 fail 不应重复刷卡片。"""
    from helper.im.progress_card import ThinkingTracker

    sends, updates = _patch_wave(monkeypatch)
    t = ThinkingTracker(receiver_id="liang.xue", receiver_id_type="user_id").start()
    t.finish("done")
    t.fail()

    assert len(updates) == 1  # 第二次 fail 被吞


def test_start_failure_does_not_raise(monkeypatch):
    """send_message 抛 WaveAPIError → start 只 log,不抛;后续 finish 走 fallback。"""
    from helper.im import progress_card
    from helper.im.progress_card import ThinkingTracker
    from helper.im.wave_client import WaveAPIError

    fallback_sends: list[dict] = []

    def boom_send(*a, **kw):
        # 第一次 start 抛;第二次 fallback 也走这个,记 args
        fallback_sends.append({"args": a, "kwargs": kw})
        if kw.get("msg_type") == "card":
            raise WaveAPIError(10401001, "bot not in chat", endpoint="/message/send")

    def fake_update(*a, **kw):
        raise AssertionError("update should not be called when no msg_id")

    monkeypatch.setattr(progress_card.wave_client, "send_message", boom_send)
    monkeypatch.setattr(progress_card.wave_client, "update_card_active", fake_update)

    t = ThinkingTracker(receiver_id="liang.xue", receiver_id_type="user_id").start()
    assert t.msg_id == ""

    # finish 没有 msg_id → 走 fallback send_message(text)
    t.finish("hello")
    # 一次 card start + 一次 text fallback
    assert len(fallback_sends) == 2
    assert fallback_sends[1]["kwargs"]["msg_type"] == "text"


def test_unsupported_id_type_skips_start(monkeypatch):
    """open_id 不能 send_message,start 直接 noop。"""
    from helper.im.progress_card import ThinkingTracker

    sends, updates = _patch_wave(monkeypatch)
    t = ThinkingTracker(receiver_id="ou_xxx", receiver_id_type="open_id").start()
    assert t.msg_id == ""
    assert sends == []
