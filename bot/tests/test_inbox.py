"""inbox.weekly.build_digest 渲染 + reply.try_handle 各分支。"""

from __future__ import annotations


def _make_inquiry(raw_id: int, question: str = "你确定吗?") -> int:
    from helper.storage import session
    from helper.storage.models import InquiryLog

    with session() as s:
        iq = InquiryLog(raw_id=raw_id, strategy_id="bound_check", question=question)
        s.add(iq)
        s.flush()
        return iq.id


def _make_spec_candidate(slug: str = "spec-a", title: str = "T") -> int:
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    with session() as s:
        sc = SpecCandidate(
            slug=slug,
            title=title,
            statement="一句话",
            cluster_raw_ids_json="[]",
            review_status="pending",
        )
        s.add(sc)
        s.flush()
        return sc.id


def test_build_digest_collects_pending_with_ids(db, settings, llm_stub, make_raw):
    """build_digest 现在会跑 memory_audit 前置 + inquiry 聚合,需要 stub 这些 LLM。"""
    from helper.inbox import build_digest

    # 没 alive memory 时 memory_audit 不会调 LLM,但 inquiry_aggregate 会(单条会跳过 LLM)
    rid = make_raw("p")
    iq_id = _make_inquiry(rid, "边界?")
    sc_id = _make_spec_candidate("foo")

    d = build_digest()
    # 单条 inquiry → 1 个独立追问组,不调 LLM 聚合(_llm_group 走 1 条快速路径)
    assert d.inquiry_groups
    assert d.inquiry_groups[0].member_ids == [iq_id]
    assert "边界" in d.inquiry_groups[0].master_question
    assert d.pending_specs
    assert d.pending_specs[0][0] == sc_id
    assert d.pending_specs[0][1] == "foo"


def test_render_card_shows_reply_hint(db, settings, llm_stub, make_raw):
    from helper.inbox import build_digest, render_card

    rid = make_raw("x")
    _make_inquiry(rid, "为什么这么定?")
    body = render_card(build_digest())
    assert "3-1" in body
    assert "答 3-N" in body


def test_render_card_empty_inbox(db, settings, llm_stub):
    from helper.inbox import build_digest, render_card

    body = render_card(build_digest())
    assert "本周 inbox 清空" in body


# ─── try_handle 分支 ─────────────────────────────────────

def test_try_handle_ignores_non_owner(db, settings):
    from helper.inbox import try_handle_reply

    # owner = "owner",其他人发同样消息 → None
    assert try_handle_reply("批准 #1", sender_domain="other", chat_id="") is None


def test_try_handle_ignores_group_chat(db, settings):
    from helper.inbox import try_handle_reply

    assert try_handle_reply("批准 #1", sender_domain="owner", chat_id="oc_xx") is None


def test_try_handle_skip(db, settings):
    from helper.inbox import try_handle_reply

    r = try_handle_reply("跳过 #99", sender_domain="owner", chat_id="")
    assert r is not None
    assert "跳过" in r.text


def test_try_handle_reject_spec(db, settings):
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    sid = _make_spec_candidate("rej")
    r = try_handle_reply(f"驳回 #{sid}", sender_domain="owner", chat_id="")
    assert r is not None
    assert "驳回" in r.text
    with session() as s:
        sc = s.get(SpecCandidate, sid)
        assert sc.review_status == "rejected"


def test_try_handle_approve_invokes_promote(db, settings, monkeypatch):
    """批准 #N → promote_spec 被调用。stub promote_spec。"""
    from helper.inbox import try_handle_reply

    sid = _make_spec_candidate("approve-me")
    called: list[tuple[str, str]] = []

    def _fake_promote(slug: str, *, reviewer: str = "") -> str:
        called.append((slug, reviewer))
        return f"specs/{slug}.md"

    monkeypatch.setattr("helper.specgen.promote_spec", _fake_promote)
    r = try_handle_reply(f"批准 #{sid}", sender_domain="owner", chat_id="")
    assert r is not None
    assert "批准" in r.text
    assert called == [("approve-me", "owner")]


def test_try_handle_explicit_answer(db, settings, make_raw):
    """『答 #N <text>』→ record_answer 写 InquiryLog.answer_raw_id + after_actions=schedule_l1。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("被追问的原 raw")
    iq_id = _make_inquiry(qrid, "为什么")
    answer_rid = make_raw("我的答复内容")

    r = try_handle_reply(
        f"答 #{iq_id} 这是答复",
        sender_domain="owner",
        chat_id="",
        answer_raw_id=answer_rid,
    )
    assert r is not None
    assert "已记录" in r.text
    assert ("schedule_l1", answer_rid) in r.after_actions

    with session() as s:
        iq = s.get(InquiryLog, iq_id)
        assert iq.answer_raw_id == answer_rid


def test_try_handle_bare_id_only_for_open_inquiry(db, settings, make_raw):
    """『#N <text>』当 N 是 open InquiryLog.id 才命中。"""
    from helper.inbox import try_handle_reply

    qrid = make_raw("q")
    iq_id = _make_inquiry(qrid)
    answer_rid = make_raw("ans")

    r = try_handle_reply(
        f"#{iq_id} 答复内容",
        sender_domain="owner",
        chat_id="",
        answer_raw_id=answer_rid,
    )
    assert r is not None
    assert ("schedule_l1", answer_rid) in r.after_actions


def test_try_handle_bare_id_unknown_falls_through(db, settings, make_raw):
    """『#9999 ...』9999 不是 open InquiryLog → 返回 None,让上层走 intent 分类。"""
    from helper.inbox import try_handle_reply

    answer_rid = make_raw("ans")
    r = try_handle_reply(
        "#9999 这只是个闲聊",
        sender_domain="owner",
        chat_id="",
        answer_raw_id=answer_rid,
    )
    assert r is None


def test_try_handle_answer_not_double_bind(db, settings, make_raw, monkeypatch):
    """已答过的 inquiry 重复『答 #N』 → 提示已答,不再 record。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("q")
    iq_id = _make_inquiry(qrid)
    first_rid = make_raw("first")
    second_rid = make_raw("second")

    try_handle_reply(
        f"答 #{iq_id} 答1", sender_domain="owner", chat_id="", answer_raw_id=first_rid
    )
    r = try_handle_reply(
        f"答 #{iq_id} 答2", sender_domain="owner", chat_id="", answer_raw_id=second_rid
    )
    assert r is not None
    assert "已答过" in r.text
    with session() as s:
        iq = s.get(InquiryLog, iq_id)
        assert iq.answer_raw_id == first_rid  # 第一次的 raw,没被覆盖


def test_try_handle_batch_skips_backquery_lines(db, settings, llm_stub, make_raw):
    """batch 回执里, "3-N 展开说说"/"3-N 讲讲" 等反问行不进 record_answer,
    走 ask 模型生成解释, 子追问留 open 下次周报继续出。"""
    import json
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InboxDigest, InquiryLog

    llm_stub.set("ask", "这条追问在问 X, 你需要回答 Y 才能 close。")

    qrid = make_raw("q")
    iq_a = _make_inquiry(qrid, "问题 A?")
    iq_b = _make_inquiry(qrid, "问题 B?")
    iq_c = _make_inquiry(qrid, "问题 C?")

    payload = {"specs": [], "conflicts": [], "inquiries": [[iq_a], [iq_b], [iq_c]]}
    with session() as s:
        s.add(InboxDigest(
            owner_domain="owner",
            items_json=json.dumps(payload, ensure_ascii=False),
        ))

    answer_rid = make_raw("批量回复")
    text = "3-1 真答案 A\n3-2 展开说说\n3-3 讲讲"
    r = try_handle_reply(text, sender_domain="owner", chat_id="", answer_raw_id=answer_rid)
    assert r is not None
    with session() as s:
        assert s.get(InquiryLog, iq_a).answer_raw_id == answer_rid
        assert s.get(InquiryLog, iq_b).answer_raw_id is None
        assert s.get(InquiryLog, iq_c).answer_raw_id is None
    assert "反向追问" in r.text
    assert "这条追问在问 X" in r.text
    # ask 被调了 2 次(3-2 + 3-3 各一次)
    assert sum(1 for c in llm_stub.calls if c[0] == "ask") == 2


def test_try_handle_batch_mixed_section_action_and_answers(db, settings, llm_stub, make_raw, monkeypatch):
    """同一条消息混合 「采纳 2-1」+ 「3-N 答复」: 都要被识别处理, 不能丢任何一条。"""
    import json
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import ConflictLog, InboxDigest, InquiryLog

    qrid = make_raw("q")
    iq_a = _make_inquiry(qrid, "问题 A?")

    with session() as s:
        cl = ConflictLog(
            raw_id=qrid, target_type="memory", target_slug="42",
            summary="新旧 directive 冲突", severity="medium",
        )
        s.add(cl)
        s.flush()
        cl_id = cl.id

    payload = {"specs": [], "conflicts": [cl_id], "inquiries": [[iq_a]]}
    with session() as s:
        s.add(InboxDigest(
            owner_domain="owner",
            items_json=json.dumps(payload, ensure_ascii=False),
        ))

    resolved: list[tuple[int, str, str]] = []
    def _fake_resolve(log_id, *, resolution, resolver_domain):
        resolved.append((log_id, resolution, resolver_domain))
        return True
    monkeypatch.setattr("helper.conflict.resolve", _fake_resolve)

    answer_rid = make_raw("批量回复")
    text = "采纳 2-1\n\n3-1 真答案 A"
    r = try_handle_reply(text, sender_domain="owner", chat_id="", answer_raw_id=answer_rid)
    assert r is not None
    assert resolved == [(cl_id, "superseded", "owner")]
    with session() as s:
        assert s.get(InquiryLog, iq_a).answer_raw_id == answer_rid


def test_send_to_calls_wave(db, settings, llm_stub, wave_send_log):
    from helper.inbox import send_to

    ok = send_to("u_owner", receiver_id_type="user_id")
    assert ok is True
    assert wave_send_log
    sent = wave_send_log[0]
    assert sent["receiver_id"] == "u_owner"
    assert "Helper 周报" in sent["content"]["text"]
