"""intent.classify — LLM 路径覆盖矩阵。

设计:语义意图全交给 LLM,不做语义启发式。这里测试:
- LLM 输出能被正确解析成 5 个 intent 之一
- 没有 unknown 类目: 空文本 / LLM 失败 / 输出无法解析 → 默认 ask
  (理由: 用户 @bot 一定有诉求, ask runtime 在低召回时会自然兜底"我不知道")
"""

from __future__ import annotations

import pytest


# (LLM 原始输出, expected_intent)— 测试 _INTENT_RE 解析能力
LLM_PARSE_CASES = [
    ("judgment", "judgment"),
    ("ask", "ask"),
    ("schedule_create", "schedule_create"),
    ("schedule_list", "schedule_list"),
    ("schedule_cancel", "schedule_cancel"),
    # 大小写 / 多余空白 / 包在句子里都该被正则抠出来
    ("Judgment", "judgment"),
    ("  ask  ", "ask"),
    ("the answer is: schedule_create", "schedule_create"),
]


@pytest.mark.parametrize("llm_out,expected", LLM_PARSE_CASES)
def test_llm_output_parse(monkeypatch, llm_out, expected):
    import helper.im.intent as I

    monkeypatch.setattr(I, "run", lambda *a, **kw: llm_out)
    assert I.classify("任意文本") == expected


def test_empty_text_defaults_to_ask(monkeypatch):
    import helper.im.intent as I

    def _no_llm(*args, **kwargs):
        raise AssertionError("LLM should not be called for empty text")

    monkeypatch.setattr(I, "run", _no_llm)
    assert I.classify("") == "ask"
    assert I.classify("   ") == "ask"


def test_llm_failure_defaults_to_ask(monkeypatch):
    """LLM 挂了 → 兜底 ask, 不再判 judgment.

    错判 ask 只是多查一次知识库, 用户拿到"我不知道"也能接受;
    错判 judgment 会把问句当资料喂 L1, 污染知识库, 代价更高.
    """
    import helper.im.intent as I

    def _raise(*args, **kwargs):
        raise RuntimeError("athenai down")

    monkeypatch.setattr(I, "run", _raise)
    assert I.classify("Helper 生产端口是 8001") == "ask"


def test_llm_garbage_output_defaults_to_ask(monkeypatch):
    import helper.im.intent as I

    monkeypatch.setattr(I, "run", lambda *a, **kw: "我也不知道你在说啥")
    # 输出里没有任何合法 intent token → 兜底 ask
    assert I.classify("奇奇怪怪的话") == "ask"
