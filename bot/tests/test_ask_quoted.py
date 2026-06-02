"""ask runtime 反查 parent_message_id (用户引用的消息) — 入参贯通 + 拼段格式。"""

from __future__ import annotations


def test_get_by_wave_msg_id_finds_row(db):
    from helper.storage import raw_store, session
    from helper.storage.models import RawInput

    with session() as s:
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            content_text="老的那条原文",
            author_domain="alice",
            chat_id="oc_g",
            wave_message_id="om_old",
        ))
        s.commit()

    with session() as s:
        row = raw_store.get_by_wave_msg_id(s, "om_old")
    assert row is not None
    assert row.content_text == "老的那条原文"


def test_get_by_wave_msg_id_empty_returns_none(db):
    from helper.storage import raw_store, session

    with session() as s:
        assert raw_store.get_by_wave_msg_id(s, "") is None
        assert raw_store.get_by_wave_msg_id(s, "om_nonexistent") is None


def test_format_quoted_message_user_message(db):
    from helper.ask.runtime import _format_quoted_message
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            content_text="IAM 网关到底怎么拿用户身份?",
            author_domain="alice",
            chat_id="oc_g",
            wave_message_id="om_quoted",
        ))
        s.commit()

    out = _format_quoted_message("om_quoted")
    assert "## 用户引用的消息" in out
    assert "用户(alice)" in out
    assert "IAM 网关到底怎么拿用户身份" in out


def test_format_quoted_message_bot_reply_marked_as_bot(db):
    """引用的是 bot 之前的回复 (im_wave_bot:* 来源) → who 显示 bot, 不是 用户(?)。"""
    from helper.ask.runtime import _format_quoted_message
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        s.add(RawInput(
            source_type="im_wave_bot:reply",
            content_text="iam_sid 不能给后端,只能在前端换 access_token。",
            author_domain="alice",  # 即便 author 是 alice, 来源决定 who
            chat_id="",
            wave_message_id="om_bot_old",
        ))
        s.commit()

    out = _format_quoted_message("om_bot_old")
    assert "bot:" in out
    assert "用户(" not in out
    assert "iam_sid 不能给后端" in out


def test_format_quoted_message_truncates_long_body(db):
    from helper.ask.runtime import _format_quoted_message
    from helper.storage import session
    from helper.storage.models import RawInput

    long_body = "X" * 2000
    with session() as s:
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            content_text=long_body,
            author_domain="alice",
            wave_message_id="om_long",
        ))
        s.commit()

    out = _format_quoted_message("om_long")
    # 800 字截断 + "…"
    assert "…" in out
    # 整体长度 < 原文
    assert len(out) < len(long_body)


def test_format_quoted_message_missing_returns_empty(db):
    from helper.ask.runtime import _format_quoted_message

    assert _format_quoted_message("") == ""
    assert _format_quoted_message("om_does_not_exist") == ""
