"""intent.classify — LLM 路径覆盖矩阵。

新设计:语义意图全交给 LLM,不做语义启发式。这里测试:
- LLM 输出能被正确解析成 6 个 intent 之一
- LLM 失败 / 输出无法解析 → unknown(不再默认 judgment)
- 空文本 → unknown
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
    ("unknown", "unknown"),
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


def test_empty_text_returns_unknown(monkeypatch):
    import helper.im.intent as I

    def _no_llm(*args, **kwargs):
        raise AssertionError("LLM should not be called for empty text")

    monkeypatch.setattr(I, "run", _no_llm)
    assert I.classify("") == "unknown"
    assert I.classify("   ") == "unknown"


def test_llm_failure_returns_unknown(monkeypatch):
    import helper.im.intent as I

    def _raise(*args, **kwargs):
        raise RuntimeError("athenai down")

    monkeypatch.setattr(I, "run", _raise)
    # LLM 失败应兜底到 unknown,不再悄悄判 judgment
    assert I.classify("Helper 生产端口是 8001") == "unknown"


def test_llm_garbage_output_returns_unknown(monkeypatch):
    import helper.im.intent as I

    monkeypatch.setattr(I, "run", lambda *a, **kw: "我也不知道你在说啥")
    # 输出里没有任何合法 intent token → unknown
    assert I.classify("奇奇怪怪的话") == "unknown"
