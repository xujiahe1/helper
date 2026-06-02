"""prefilter 疑问句硬规则 — 防止问句污染检索池。

历史污染:prompt v2 后 mini LLM 把 "刘佳翔就是哥吗?" 之类问句判成 yes,
L1 抽成 section 进 fts/vector 召回池。修复:should_run_l1 在 keyword/llm 之前
先判 is_question,命中直接 (False, "question")。
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "text",
    [
        "刘佳翔就是哥吗?",
        "刘佳翔就是哥吗?",  # 中文问号
        "谁是哥?",
        "为什么这么决定?",
        "能不能改一下?",
        "怎么办?",
        "什么是 IAM 网关",  # 句首疑问代词,无问号也算
        "哪些人在用这个?",
        "决定不做了吗?",  # 含信号词的问句也不抽
    ],
)
def test_is_question_positive(text):
    from helper.ingest.prefilter import is_question
    assert is_question(text) is True, f"应判为问句: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "决定把首页改成新版",
        "上周敲定方案 A",
        "已经上线了",
        "",
        "  ",
        "好的收到",
        "刘佳翔今天提了 PR",  # 含人名但是陈述句
    ],
)
def test_is_question_negative(text):
    from helper.ingest.prefilter import is_question
    assert is_question(text) is False, f"不应判为问句: {text!r}"


def test_should_run_l1_question_short_circuits_keyword(monkeypatch):
    """问句即便含 SIGNAL_KEYWORDS 也不抽 L1 — 'question' tag 优先于 'keyword'。"""
    from helper.ingest import prefilter

    # 不该走到 llm_screen
    def _boom(*a, **kw):
        raise AssertionError("llm_screen should not be called for questions")
    monkeypatch.setattr(prefilter, "llm_screen", _boom)

    run, reason = prefilter.should_run_l1("决定不做这个功能了吗?")
    assert run is False
    assert reason == "question"


def test_should_run_l1_keyword_still_works():
    from helper.ingest.prefilter import should_run_l1

    run, reason = should_run_l1("决定把首页改成新版")
    assert run is True
    assert reason == "keyword"


def test_should_run_l1_llm_path_for_ambiguous(monkeypatch):
    """无疑问 + 无关键词 → 走 mini LLM 兜底。"""
    from helper.ingest import prefilter

    monkeypatch.setattr(prefilter, "llm_screen", lambda t: True)
    run, reason = prefilter.should_run_l1("把这个调到下周")
    assert run is True
    assert reason == "llm_yes"

    monkeypatch.setattr(prefilter, "llm_screen", lambda t: False)
    run, reason = prefilter.should_run_l1("把这个调到下周")
    assert run is False
    assert reason == "llm_no"
