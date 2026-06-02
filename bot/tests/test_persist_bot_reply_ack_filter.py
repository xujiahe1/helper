"""_persist_bot_reply 入口 ack 过滤 — 纯系统 ack 不该落 raw,
实质答复和 ❌ 拒答仍然落。"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "ack_text",
    [
        "🤔 没看出能沉淀的内容,这条没入库",
        "🤔 没明白你的意图,可以换个说法",
        "🔄 《IAM 接入指南》已重新学习 (raw#215)\n(共抽出 13 条原子)",
        "🔄 已咨询 @tachi, 等回复中...",
        "✓ 已记录(7 条原子)",
        "",  # 空文本本来就 return
    ],
)
def test_persist_bot_reply_skips_ack(db, settings, ack_text):
    from helper.im.wave_actions import _persist_bot_reply
    from helper.storage import session
    from helper.storage.models import RawInput

    _persist_bot_reply(
        text=ack_text,
        receiver_domain="alice",
        chat_id="oc_g",
        parent_message_id="om_user",
        bot_msg_id=f"om_ack_{hash(ack_text) % 10000}",
    )
    with session() as s:
        cnt = s.query(RawInput).filter(RawInput.source_type == "im_wave_bot").count()
    assert cnt == 0, f"ack 文案不该落 raw: {ack_text!r}"


@pytest.mark.parametrize(
    "real_text",
    [
        "你的问题问的是 IAM 网关接入文档里 iam_sid 怎么换成用户身份",
        "❌ 我没权限读这篇文档,你可以把正文贴给我",
        "根据'哥'的风格,他让你们去摸清楚业务规则",
        "Translation request: How can iam_sid be exchanged",
    ],
)
def test_persist_bot_reply_keeps_real_replies(db, settings, real_text):
    """实质答复和 ❌ 拒答都该落 raw (有 chat_context 价值)。"""
    from helper.im.wave_actions import _persist_bot_reply
    from helper.storage import session
    from helper.storage.models import L1Result, RawInput

    _persist_bot_reply(
        text=real_text,
        receiver_domain="alice",
        chat_id="oc_g",
        parent_message_id="om_user",
        bot_msg_id=f"om_real_{hash(real_text) % 10000}",
    )
    with session() as s:
        rows = s.query(RawInput).filter(RawInput.source_type == "im_wave_bot").all()
        assert len(rows) == 1, f"实质答复必须落 raw: {real_text!r}"
        # 同时确认 #8 的 skipped 标仍然挂上 (回归)
        lr = s.get(L1Result, rows[0].id)
        assert lr is not None
        assert lr.error == "skipped:bot_reply"


def test_is_ack_text_unit():
    """直接验证 _is_ack_text 判定函数。"""
    from helper.im.wave_actions import _is_ack_text

    # ack
    assert _is_ack_text("🤔 没看出能沉淀的内容") is True
    assert _is_ack_text("🔄 《X》已重新学习 (raw#1) (共抽出 5 条原子)") is True
    assert _is_ack_text("✓ 已记录(3 条原子)") is True
    assert _is_ack_text("") is True
    assert _is_ack_text("   ") is True  # 全空白也算

    # 非 ack
    assert _is_ack_text("❌ 我没权限读这篇文档") is False
    assert _is_ack_text("根据检索结果, IAM 网关...") is False
    assert _is_ack_text("🤔 思考中...") is True  # 这是 ack (progress card 不该走这, 但前缀防御)
