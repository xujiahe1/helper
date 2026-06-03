"""上下文机制 — 单聊兜底 / format_context_block / intent classify 注入。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest


def _make_msg(s, *, source_type, content_text, author_domain, chat_id="", offset_minutes=0):
    """建一条 raw_inputs(显式控制 created_at,模拟历史时间)。"""
    from helper.storage.models import RawInput

    r = RawInput(
        source_type=source_type,
        content_text=content_text,
        author_domain=author_domain,
        chat_id=chat_id,
    )
    s.add(r)
    s.flush()
    if offset_minutes:
        r.created_at = datetime.utcnow() - timedelta(minutes=offset_minutes)
        s.flush()
    return r.id


def test_list_chat_history_solo_chat_fallback(db):
    """单聊(chat_id="") + fallback_author 能拉到该用户消息 + bot 给该用户的回复。"""
    from helper.storage import raw_store, session

    with session() as s:
        _make_msg(s, source_type="im_wave_msg", content_text="user say A", author_domain="alice")
        _make_msg(s, source_type="im_wave_bot", content_text="bot reply A", author_domain="alice")
        _make_msg(s, source_type="im_wave_msg", content_text="bob say X", author_domain="bob")
        s.commit()

    with session() as s:
        rows = raw_store.list_chat_history(s, "", fallback_author="alice")

    assert [r.content_text for r in rows] == ["user say A", "bot reply A"]


def test_list_chat_history_returns_empty_when_no_chat_or_author(db):
    from helper.storage import raw_store, session

    with session() as s:
        rows = raw_store.list_chat_history(s, "")
    assert rows == []


def test_context_cutoff_filters_old_raws_in_group(db):
    """群里 /clear 钉了 cutoff 后, 拉历史只返回 cutoff 之后的消息(老 raw 不删)。"""
    from helper.storage import raw_store, session

    with session() as s:
        old1 = _make_msg(s, source_type="im_wave_msg", content_text="old A",
                         author_domain="alice", chat_id="oc_g")
        old2 = _make_msg(s, source_type="im_wave_msg", content_text="old B",
                         author_domain="bob", chat_id="oc_g")
        # /clear 钉到 old2
        raw_store.set_context_cutoff(s, "oc_g", old2)
        _make_msg(s, source_type="im_wave_msg", content_text="new C",
                  author_domain="alice", chat_id="oc_g")
        s.commit()

    with session() as s:
        rows = raw_store.list_chat_history(s, "oc_g")
    assert [r.content_text for r in rows] == ["new C"]
    # 老数据物理还在
    with session() as s:
        from helper.storage.models import RawInput
        assert s.query(RawInput).filter_by(chat_id="oc_g").count() == 3


def test_context_cutoff_filters_old_raws_in_dm(db):
    """私聊 scope: user:<domain>。"""
    from helper.storage import raw_store, session

    with session() as s:
        last_old = _make_msg(s, source_type="im_wave_msg", content_text="old user",
                             author_domain="alice")
        _make_msg(s, source_type="im_wave_bot", content_text="old bot reply",
                  author_domain="alice")
        # 钉到当前最大 raw
        last = s.query(raw_store.RawInput.id).order_by(raw_store.RawInput.id.desc()).first()[0]
        raw_store.set_context_cutoff(s, "user:alice", last)
        _make_msg(s, source_type="im_wave_msg", content_text="post-clear", author_domain="alice")
        s.commit()

    with session() as s:
        rows = raw_store.list_chat_history(s, "", fallback_author="alice")
    assert [r.content_text for r in rows] == ["post-clear"]


def test_set_context_cutoff_upsert_keeps_latest(db):
    from helper.storage import raw_store, session

    with session() as s:
        raw_store.set_context_cutoff(s, "oc_g", 5)
        raw_store.set_context_cutoff(s, "oc_g", 12)
        s.commit()
    with session() as s:
        assert raw_store.get_context_cutoff(s, "oc_g") == 12


def test_format_context_block_renders_user_and_bot(db):
    from helper.storage import raw_store, session

    with session() as s:
        _make_msg(s, source_type="im_wave_msg",
                  content_text="去学一下这个文档", author_domain="alice", offset_minutes=10)
        _make_msg(s, source_type="im_wave_bot",
                  content_text="❌ 我没权限", author_domain="alice", offset_minutes=9)
        _make_msg(s, source_type="im_wave_msg",
                  content_text="再读一下试试", author_domain="alice", offset_minutes=1)
        s.commit()

    with session() as s:
        block = raw_store.format_context_block(s, chat_id="", fallback_author="alice")

    assert "## 历史对话" in block
    # 用户行带 (alice)
    assert "用户(alice)" in block
    # bot 行
    assert "bot:" in block
    # 三条都在
    assert "去学一下" in block
    assert "没权限" in block
    assert "再读一下" in block


def test_format_context_block_window_excludes_old_messages(db):
    """超过窗口(默认 1 天)的消息不进上下文。"""
    from helper.storage import raw_store, session

    with session() as s:
        _make_msg(s, source_type="im_wave_msg",
                  content_text="三天前的话", author_domain="alice", offset_minutes=3 * 24 * 60)
        _make_msg(s, source_type="im_wave_msg",
                  content_text="刚才的话", author_domain="alice", offset_minutes=5)
        s.commit()

    with session() as s:
        block = raw_store.format_context_block(s, chat_id="", fallback_author="alice")

    assert "刚才的话" in block
    assert "三天前的话" not in block


def test_format_context_block_excludes_current_raw(db):
    from helper.storage import raw_store, session

    with session() as s:
        _make_msg(s, source_type="im_wave_msg",
                  content_text="历史 1", author_domain="alice", offset_minutes=5)
        current = _make_msg(s, source_type="im_wave_msg",
                            content_text="DO_NOT_INCLUDE_ME", author_domain="alice")
        s.commit()

    with session() as s:
        block = raw_store.format_context_block(
            s, chat_id="", fallback_author="alice", exclude_raw_id=current,
        )

    assert "历史 1" in block
    assert "DO_NOT_INCLUDE_ME" not in block


def test_intent_classify_passes_context_into_prompt(monkeypatch):
    """classify(text, chat_context=...) 应该把上下文段拼进 user_msg 给 LLM。"""
    import helper.im.intent as I

    captured = {}

    def fake_run(task, *, system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return "ask"

    monkeypatch.setattr(I, "run", fake_run)

    ctx = "## 历史对话\n[12:00] 用户(alice): 去学一下这个文档\n[12:00] bot: ❌ 我没权限"
    result = I.classify("再读一下试试", chat_context=ctx)

    assert result == "ask"
    # 历史段在 user_msg 里
    assert "历史对话" in captured["user"]
    assert "再读一下试试" in captured["user"]
    # system 提到了"附了「历史对话」"
    assert "历史对话" in captured["system"]


def test_intent_classify_without_context_unchanged(monkeypatch):
    """不传 chat_context 时,user_msg 应该就是原文(不含上下文段)。"""
    import helper.im.intent as I

    captured = {}

    def fake_run(task, *, system, user, **kw):
        captured["user"] = user
        return "judgment"

    monkeypatch.setattr(I, "run", fake_run)
    I.classify("PRD 模板风险章节怎么写")
    assert captured["user"] == "PRD 模板风险章节怎么写"
    assert "历史对话" not in captured["user"]
