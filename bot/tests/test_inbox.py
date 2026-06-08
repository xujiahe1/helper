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
    # 顶部固定提示「周报裁判回执」触发词
    assert "周报裁判回执" in body


def test_render_card_empty_inbox(db, settings, llm_stub):
    from helper.inbox import build_digest, render_card

    body = render_card(build_digest())
    assert "本周 inbox 清空" in body


# ─── try_handle 分支 ─────────────────────────────────────

def _seed_digest(payload: dict) -> None:
    """把固定 payload 写到 InboxDigest 里, 方便 try_handle 解析。"""
    import json
    from helper.storage import session
    from helper.storage.models import InboxDigest

    with session() as s:
        s.add(InboxDigest(
            owner_domain="owner",
            items_json=json.dumps(payload, ensure_ascii=False),
        ))


def test_try_handle_ignores_non_owner(db, settings):
    from helper.inbox import try_handle_reply

    assert try_handle_reply(
        "周报裁判回执\n批准 1-1", sender_domain="other", chat_id=""
    ) is None


def test_try_handle_ignores_group_chat(db, settings):
    from helper.inbox import try_handle_reply

    assert try_handle_reply(
        "周报裁判回执\n批准 1-1", sender_domain="owner", chat_id="oc_xx"
    ) is None


def test_try_handle_no_trigger_returns_none(db, settings, make_raw):
    """没有「周报裁判回执」触发词 → 一律放过, 让上层走闲聊路径。
    这是新设计的核心: 把"3-1 等于 2"这种闲聊和真裁决分开, 不再靠多行启发式。"""
    from helper.inbox import try_handle_reply

    qrid = make_raw("q")
    iq_id = _make_inquiry(qrid, "?")
    _seed_digest({"specs": [], "conflicts": [], "inquiries": [[iq_id]]})

    answer_rid = make_raw("a")
    # 单行 "3-1 xxx" 没触发词, 必须放过
    r = try_handle_reply(
        "3-1 这是答案",
        sender_domain="owner", chat_id="", answer_raw_id=answer_rid,
    )
    assert r is None

    # "采纳 2-1" 没触发词, 也放过
    r = try_handle_reply("采纳 2-1", sender_domain="owner", chat_id="")
    assert r is None


def test_try_handle_trigger_with_no_body(db, settings):
    """只有触发词, 没正文 → 给提示而非静默 None。"""
    from helper.inbox import try_handle_reply

    r = try_handle_reply("周报裁判回执", sender_domain="owner", chat_id="")
    assert r is not None
    assert "没有内容" in r.text


def test_try_handle_trigger_with_no_recognized_lines(db, settings):
    """触发词存在但每行都不是合法指令 → 给格式提示。"""
    from helper.inbox import try_handle_reply

    r = try_handle_reply(
        "周报裁判回执\n你好啊\n这是闲聊",
        sender_domain="owner", chat_id="",
    )
    assert r is not None
    assert "没识别出指令" in r.text


def test_try_handle_skip_spec(db, settings):
    """跳过 1-N → 走 spec 跳过路径。"""
    from helper.inbox import try_handle_reply

    sid = _make_spec_candidate("skip-me")
    _seed_digest({"specs": [sid], "conflicts": [], "inquiries": []})

    r = try_handle_reply(
        "周报裁判回执\n跳过 1-1", sender_domain="owner", chat_id="",
    )
    assert r is not None
    assert "跳过" in r.text


def test_try_handle_reject_spec(db, settings):
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import SpecCandidate

    sid = _make_spec_candidate("rej")
    _seed_digest({"specs": [sid], "conflicts": [], "inquiries": []})

    r = try_handle_reply(
        "周报裁判回执\n驳回 1-1", sender_domain="owner", chat_id="",
    )
    assert r is not None
    assert "驳回" in r.text
    with session() as s:
        sc = s.get(SpecCandidate, sid)
        assert sc.review_status == "rejected"


def test_try_handle_approve_invokes_promote(db, settings, monkeypatch):
    """批准 1-N → promote_spec 被调用。"""
    from helper.inbox import try_handle_reply

    sid = _make_spec_candidate("approve-me")
    _seed_digest({"specs": [sid], "conflicts": [], "inquiries": []})

    called: list[tuple[str, str]] = []

    def _fake_promote(slug: str, *, reviewer: str = "") -> str:
        called.append((slug, reviewer))
        return f"specs/{slug}.md"

    monkeypatch.setattr("helper.specgen.promote_spec", _fake_promote)
    r = try_handle_reply(
        "周报裁判回执\n批准 1-1", sender_domain="owner", chat_id="",
    )
    assert r is not None
    assert "批准" in r.text
    assert called == [("approve-me", "owner")]


def test_try_handle_single_answer(db, settings, make_raw):
    """单行 「3-N <答案>」 在带触发词时也能命中, record_answer + schedule_l1。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("被追问的原 raw")
    iq_id = _make_inquiry(qrid, "为什么")
    _seed_digest({"specs": [], "conflicts": [], "inquiries": [[iq_id]]})

    answer_rid = make_raw("我的答复内容")
    r = try_handle_reply(
        "周报裁判回执\n3-1 这是答复",
        sender_domain="owner",
        chat_id="",
        answer_raw_id=answer_rid,
    )
    assert r is not None
    assert ("schedule_l1", answer_rid) in r.after_actions
    with session() as s:
        iq = s.get(InquiryLog, iq_id)
        assert iq.answer_raw_id == answer_rid


def test_try_handle_answer_not_double_bind(db, settings, make_raw, monkeypatch):
    """已答过的 inquiry 重复回 「3-N 答复」 → 提示已答, 不覆盖原 raw。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InquiryLog

    qrid = make_raw("q")
    iq_id = _make_inquiry(qrid)
    _seed_digest({"specs": [], "conflicts": [], "inquiries": [[iq_id]]})

    first_rid = make_raw("first")
    second_rid = make_raw("second")

    try_handle_reply(
        "周报裁判回执\n3-1 答1",
        sender_domain="owner", chat_id="", answer_raw_id=first_rid,
    )
    r = try_handle_reply(
        "周报裁判回执\n3-1 答2",
        sender_domain="owner", chat_id="", answer_raw_id=second_rid,
    )
    assert r is not None
    with session() as s:
        iq = s.get(InquiryLog, iq_id)
        assert iq.answer_raw_id == first_rid


def test_try_handle_batch_skips_backquery_lines(db, settings, llm_stub, make_raw):
    """batch 回执里, "3-N 展开说说"/"3-N 讲讲" 等反问行不进 record_answer,
    走 ask 模型生成解释, 子追问留 open 下次周报继续出。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import InquiryLog

    llm_stub.set("ask", "这条追问在问 X, 你需要回答 Y 才能 close。")

    qrid = make_raw("q")
    iq_a = _make_inquiry(qrid, "问题 A?")
    iq_b = _make_inquiry(qrid, "问题 B?")
    iq_c = _make_inquiry(qrid, "问题 C?")
    _seed_digest({"specs": [], "conflicts": [], "inquiries": [[iq_a], [iq_b], [iq_c]]})

    answer_rid = make_raw("批量回复")
    text = "周报裁判回执\n3-1 真答案 A\n3-2 展开说说\n3-3 讲讲"
    r = try_handle_reply(text, sender_domain="owner", chat_id="", answer_raw_id=answer_rid)
    assert r is not None
    with session() as s:
        assert s.get(InquiryLog, iq_a).answer_raw_id == answer_rid
        assert s.get(InquiryLog, iq_b).answer_raw_id is None
        assert s.get(InquiryLog, iq_c).answer_raw_id is None
    assert "反向追问" in r.text
    assert "这条追问在问 X" in r.text
    assert sum(1 for c in llm_stub.calls if c[0] == "ask") == 2


def test_try_handle_batch_mixed_section_action_and_answers(db, settings, llm_stub, make_raw, monkeypatch):
    """同一条消息混合 「采纳 2-1」+ 「3-N 答复」: 都要被识别处理, 不能丢任何一条。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import ConflictLog, InquiryLog

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

    _seed_digest({"specs": [], "conflicts": [cl_id], "inquiries": [[iq_a]]})

    resolved: list[tuple[int, str, str]] = []
    def _fake_resolve(log_id, *, resolution, resolver_domain):
        resolved.append((log_id, resolution, resolver_domain))
        return True
    monkeypatch.setattr("helper.conflict.resolve", _fake_resolve)

    answer_rid = make_raw("批量回复")
    text = "周报裁判回执\n采纳 2-1\n\n3-1 真答案 A"
    r = try_handle_reply(text, sender_domain="owner", chat_id="", answer_raw_id=answer_rid)
    assert r is not None
    assert resolved == [(cl_id, "superseded", "owner")]
    with session() as s:
        assert s.get(InquiryLog, iq_a).answer_raw_id == answer_rid


def test_try_handle_section_action_reverse_form(db, settings, monkeypatch):
    """「2-1 采纳」(编号在前) 这种写法 raw#527 用过, 必须保留。"""
    from helper.inbox import try_handle_reply
    from helper.storage import session
    from helper.storage.models import ConflictLog

    with session() as s:
        cl = ConflictLog(
            raw_id=0, target_type="memory", target_slug="9",
            summary="x", severity="low",
        )
        s.add(cl)
        s.flush()
        cl_id = cl.id

    _seed_digest({"specs": [], "conflicts": [cl_id], "inquiries": []})

    resolved: list[tuple[int, str]] = []
    def _fake_resolve(log_id, *, resolution, resolver_domain):
        resolved.append((log_id, resolution))
        return True
    monkeypatch.setattr("helper.conflict.resolve", _fake_resolve)

    r = try_handle_reply(
        "周报裁判回执\n2-1 采纳", sender_domain="owner", chat_id="",
    )
    assert r is not None
    assert resolved == [(cl_id, "superseded")]


def test_send_to_calls_wave(db, settings, llm_stub, wave_send_log):
    from helper.inbox import send_to

    ok = send_to("u_owner", receiver_id_type="user_id")
    assert ok is True
    assert wave_send_log
    sent = wave_send_log[0]
    assert sent["receiver_id"] == "u_owner"
    assert "Helper 周报" in sent["content"]["text"]
