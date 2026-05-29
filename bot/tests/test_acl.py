"""M8 Topic ACL — 打标 / 出口过滤 / 入口短路 路径覆盖。"""

from __future__ import annotations

import pytest


@pytest.fixture
def acl_reset():
    """每个测试结束清 acl cache, 避免 yaml 状态串。"""
    from helper.acl import reset_acl_cache
    reset_acl_cache()
    yield
    reset_acl_cache()


# ---------- 打标 ----------


def test_tag_text_returns_topic_id_when_llm_says_ge(db, settings, llm_stub, acl_reset):
    from helper.acl import tag_text

    llm_stub.set("acl_tag", "ge")
    assert tag_text("螃蟹是哥对周婷的私下称呼") == "ge"


def test_tag_text_returns_empty_when_llm_says_public(db, settings, llm_stub, acl_reset):
    from helper.acl import tag_text

    llm_stub.set("acl_tag", "")
    assert tag_text("可见性怎么配置") == ""


def test_tag_text_uncertain_uses_default_on_uncertain(db, settings, llm_stub, acl_reset):
    """LLM 返 UNCERTAIN → 用 yaml.default_on_uncertain (default 是空串)。"""
    from helper.acl import tag_text

    llm_stub.set("acl_tag", "UNCERTAIN")
    assert tag_text("模糊话题") == ""  # default_on_uncertain="" 时


def test_tag_text_unknown_id_falls_back_to_default(db, settings, llm_stub, acl_reset):
    """LLM 返了 yaml 里没有的 topic_id → 当不确定走 default。"""
    from helper.acl import tag_text

    llm_stub.set("acl_tag", "made_up_topic")
    assert tag_text("测试") == ""


def test_tag_text_llm_failure_retries_then_default(db, settings, llm_stub, acl_reset):
    """LLM 两次都 raise → fallback default_on_uncertain。"""
    from helper.acl import tag_text

    calls = {"n": 0}

    def _boom(**kw):
        calls["n"] += 1
        raise RuntimeError("athenai down")

    llm_stub.set("acl_tag", _boom)
    assert tag_text("xxx") == ""  # default 是空串
    assert calls["n"] == 2  # 重试 1 次


def test_tag_raw_propagates_to_candidates(db, settings, llm_stub, acl_reset):
    """tag_raw 给 raw 落标 + 同步派生 entity / fact / case 候选(继承同 topic)。"""
    import json
    from helper.acl import tag_raw
    from helper.storage import session
    from helper.storage.models import (
        CaseCandidate, EntityCandidate, FactCandidate, L1Item, RawInput,
    )

    with session() as s:
        r = RawInput(source_type="im_wave", content_text="螃蟹是哥对周婷的称呼", author_domain="alice")
        s.add(r)
        s.flush()
        raw_id = r.id
        s.add(L1Item(raw_id=raw_id, idx=0, type="fact", payload_json="{}"))
        s.add(EntityCandidate(slug="螃蟹", name="螃蟹", description="...", raw_refs_json=json.dumps([raw_id])))
        s.add(FactCandidate(slug="ge_called_screen_crab", statement="...", raw_refs_json=json.dumps([[raw_id, 0]])))
        s.add(CaseCandidate(slug="case1", title="x", raw_refs_json=json.dumps([raw_id])))

    llm_stub.set("acl_tag", "ge")
    topic = tag_raw(raw_id)
    assert topic == "ge"

    with session() as s:
        assert s.get(RawInput, raw_id).acl_topic_id == "ge"
        assert s.query(L1Item).filter_by(raw_id=raw_id).first().acl_topic_id == "ge"
        assert s.query(EntityCandidate).filter_by(slug="螃蟹").first().acl_topic_id == "ge"
        assert s.query(FactCandidate).filter_by(slug="ge_called_screen_crab").first().acl_topic_id == "ge"
        assert s.query(CaseCandidate).filter_by(slug="case1").first().acl_topic_id == "ge"


def test_tag_raw_propagation_does_not_match_substring(db, settings, llm_stub, acl_reset):
    """raw_id=1 的标不能错误地继承到 raw_refs_json=[12, 21] 的候选(数字边界)。"""
    import json
    from helper.acl import tag_raw
    from helper.storage import session
    from helper.storage.models import EntityCandidate, RawInput

    with session() as s:
        # 真正引用 raw_id=1
        r1 = RawInput(source_type="im_wave", content_text="哥的事", author_domain="a")
        s.add(r1); s.flush()
        rid = r1.id
        s.add(EntityCandidate(slug="hit", name="hit", raw_refs_json=json.dumps([rid])))
        # 只是数字 12 / 21 包含 "1" 字符,不能误中
        s.add(EntityCandidate(
            slug="miss", name="miss",
            raw_refs_json=json.dumps([rid + 100, rid + 1000]),
        ))

    llm_stub.set("acl_tag", "ge")
    tag_raw(rid)

    with session() as s:
        assert s.query(EntityCandidate).filter_by(slug="hit").first().acl_topic_id == "ge"
        assert s.query(EntityCandidate).filter_by(slug="miss").first().acl_topic_id == ""


# ---------- 出口过滤 ----------


def test_filter_hits_blocks_non_whitelisted_asker(db, settings, llm_stub, acl_reset):
    """带 ge 标的 raw / entity hit 对非白名单用户不可见。"""
    from helper.acl import filter_hits
    from helper.ask.retrieve import Hit
    from helper.storage import session
    from helper.storage.models import EntityCandidate, RawInput

    with session() as s:
        r1 = RawInput(source_type="im_wave", content_text="public", acl_topic_id="")
        r2 = RawInput(source_type="im_wave", content_text="ge stuff", acl_topic_id="ge")
        s.add_all([r1, r2]); s.flush()
        rid_pub, rid_ge = r1.id, r2.id
        s.add(EntityCandidate(slug="哥", name="哥", description="...", acl_topic_id="ge"))
        s.add(EntityCandidate(slug="iam", name="iam", description="...", acl_topic_id=""))

    hits = [
        Hit(type="raw", ref=str(rid_pub), title="", body="", score=1.0),
        Hit(type="raw", ref=str(rid_ge), title="", body="", score=0.9),
        Hit(type="entity", ref="哥", title="", body="", score=0.8),
        Hit(type="entity", ref="iam", title="", body="", score=0.7),
    ]

    # 非白名单
    allowed, blocked = filter_hits("outsider.user", hits)
    allowed_keys = {(h.type, h.ref) for h in allowed}
    assert allowed_keys == {("raw", str(rid_pub)), ("entity", "iam")}
    assert {(h.type, h.ref) for h in blocked} == {("raw", str(rid_ge)), ("entity", "哥")}

    # 白名单
    allowed2, blocked2 = filter_hits("jiahe.xu", hits)
    assert len(allowed2) == 4 and not blocked2


def test_retrieve_relevant_filters_by_acl(db, settings, llm_stub, acl_reset, monkeypatch):
    """end-to-end: retrieve_relevant 出口对非白名单用户过滤掉敏感 hit。"""
    from helper.ask.retrieve import Hit, retrieve_relevant
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        r_ge = RawInput(source_type="im_wave", content_text="哥的私事", acl_topic_id="ge")
        r_pub = RawInput(source_type="im_wave", content_text="iam 问题", acl_topic_id="")
        s.add_all([r_ge, r_pub]); s.flush()
        rid_ge, rid_pub = r_ge.id, r_pub.id

    # 直接绕过 fts/vector 路径, 注入两条 hit 让 ACL 过滤来过
    fake_hits = [
        Hit(type="raw", ref=str(rid_ge), title="", body="x", score=1.0, sources=["fts"]),
        Hit(type="raw", ref=str(rid_pub), title="", body="y", score=0.9, sources=["fts"]),
    ]
    monkeypatch.setattr("helper.ask.retrieve._fts_pass", lambda q, skip: fake_hits)
    monkeypatch.setattr("helper.ask.retrieve._vector_pass", lambda q, skip: [])
    monkeypatch.setattr("helper.ask.retrieve._bundle_jaccard_pass", lambda toks: [])

    # 非白名单 → 只看到公开
    out = retrieve_relevant("xxx", asker_domain="outsider")
    refs = {h.ref for h in out}
    assert refs == {str(rid_pub)}

    # 不传 asker → 不过滤(向后兼容,工具调用方如 conflict 检测)
    out2 = retrieve_relevant("xxx")
    assert {h.ref for h in out2} == {str(rid_ge), str(rid_pub)}


# ---------- ask 入口短路 ----------


def test_deny_for_question_blocks_when_question_hits_topic(
    db, settings, llm_stub, retrieve_stub, acl_reset, stub_bundle, monkeypatch
):
    """问题文本 + 历史命中 ge 且 asker 非白名单 → ask 返 deny_response, 不调主路径。"""
    from helper.ask import ask

    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    # acl_tag 命中 ge
    llm_stub.set("acl_tag", "ge")
    # 主路径 ask 不应被调用 — 没注册 stub, 调到就 AssertionError
    ans = ask("谁是螃蟹", asker_domain="outsider", chat_id="oc1")
    assert ans.answer == "这个话题我不知道。"
    assert ans.confidence == "low"


def test_deny_for_question_passes_through_for_whitelist(
    db, settings, llm_stub, retrieve_stub, acl_reset, stub_bundle, monkeypatch
):
    """白名单用户问敏感问题 → 不拦, 走主路径回答。"""
    from helper.ask import ask

    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    llm_stub.set("acl_tag", "ge")
    llm_stub.set("ask", "## 答复\n这是答\n\n## 置信度\nhigh\n\n## 引用\n")

    ans = ask("谁是螃蟹", asker_domain="jiahe.xu", chat_id="oc1")
    assert ans.answer == "这是答"
    assert ans.confidence == "high"


def test_deny_for_question_no_block_for_public_question(
    db, settings, llm_stub, retrieve_stub, acl_reset, stub_bundle, monkeypatch
):
    """非敏感问题, 任何用户都不拦。"""
    from helper.ask import ask

    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    llm_stub.set("acl_tag", "")
    llm_stub.set("ask", "## 答复\nIAM\n\n## 置信度\nlow\n\n## 引用\n")

    ans = ask("可见性怎么配置", asker_domain="outsider")
    assert ans.answer == "IAM"


# ---------- 出口硬过滤 scrub_output ----------


def test_scrub_output_replaces_when_blocklist_term_appears(db, settings, acl_reset):
    """非白名单 asker, 答案里出现"刘佳翔" → 整段替换为 deny_response。"""
    from helper.acl import scrub_output

    out = scrub_output("outsider", "据信刘佳翔最近正在推 IAM 合规改造。")
    assert out == "这个话题我不知道。"


def test_scrub_output_passes_through_for_whitelist(db, settings, acl_reset):
    """白名单 asker 看到包含"刘佳翔"的答案 → 不替换。"""
    from helper.acl import scrub_output

    out = scrub_output("jiahe.xu", "据信刘佳翔最近正在推 IAM 合规改造。")
    assert out is None


def test_scrub_output_no_op_when_no_term(db, settings, acl_reset):
    """没命中黑词 → 返 None, 调用方保留原 text。"""
    from helper.acl import scrub_output

    assert scrub_output("outsider", "IAM 可见性配置在 admin 后台。") is None


def test_ask_scrubs_answer_when_llm_leaks_blocked_name(
    db, settings, llm_stub, retrieve_stub, acl_reset, stub_bundle, monkeypatch
):
    """LLM 凭参数知识自己说出"刘佳翔" → ask 末尾 scrub_output 整段替换。"""
    from helper.ask import ask

    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    llm_stub.set("acl_tag", "")  # 入口闸不命中(模拟问题表述无敏感)
    llm_stub.set("ask", "## 答复\n据我所知刘佳翔目前在主导 IAM 改造。\n\n## 置信度\nlow\n\n## 引用\n")

    ans = ask("信息化负责人最近在干嘛", asker_domain="outsider")
    assert ans.answer == "这个话题我不知道。"
    assert ans.confidence == "low"
    assert ans.citations == []


# ---------- chat_context 按 asker 过滤 ----------


def test_chat_context_filters_ge_history_for_outsider(db, settings, acl_reset):
    """白名单用户连续聊 ge 后, 非白名单 asker 拿到的群历史里 ge raw 全跳过。"""
    from helper.storage import session
    from helper.storage import raw_store
    from helper.storage.models import RawInput

    with session() as s:
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            content_text="哥最近喷谁了", author_domain="jiahe.xu",
            chat_id="oc1", acl_topic_id="ge",
        ))
        s.add(RawInput(
            source_type="im_wave_bot",
            content_text="哥喷了周婷, 说她是螃蟹", author_domain="jiahe.xu",
            chat_id="oc1", acl_topic_id="ge",
        ))
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            content_text="iam 怎么配置", author_domain="bob",
            chat_id="oc1", acl_topic_id="",
        ))

    # outsider 看历史 — 只剩公开那条
    with session() as s:
        block = raw_store.format_context_block(
            s, chat_id="oc1", asker_domain="outsider",
        )
    assert "螃蟹" not in block
    assert "周婷" not in block
    assert "iam 怎么配置" in block

    # 白名单看历史 — 三条全在
    with session() as s:
        block2 = raw_store.format_context_block(
            s, chat_id="oc1", asker_domain="jiahe.xu",
        )
    assert "螃蟹" in block2
    assert "iam 怎么配置" in block2

    # 不传 asker — 不过滤(向后兼容 L1 抽取等内部场景)
    with session() as s:
        block3 = raw_store.format_context_block(s, chat_id="oc1")
    assert "螃蟹" in block3


# ---------- backfill ----------


def test_backfill_all_walks_all_raws_and_can_repeat(db, settings, llm_stub, acl_reset):
    from helper.acl import backfill_all
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        for txt in ["公开 1", "公开 2", "哥的事"]:
            s.add(RawInput(source_type="im_wave", content_text=txt))

    # 第三条返 ge,前两条空
    seq = iter(["", "", "ge"])
    llm_stub.set("acl_tag", lambda **kw: next(seq))

    n = backfill_all()
    assert n == 3

    with session() as s:
        rows = sorted(s.query(RawInput).all(), key=lambda r: r.id)
        assert [r.acl_topic_id for r in rows] == ["", "", "ge"]
