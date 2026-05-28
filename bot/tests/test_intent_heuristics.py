"""intent.classify — 启发式覆盖矩阵。

设计:列一张 case 表,期望全部走启发式不调 LLM。运行时 stub 掉 helper.im.intent.run
让任意 LLM 调用直接抛 — 命中即说明被启发式拦下。
"""

from __future__ import annotations

import pytest


# (text, expected_intent)
HEURISTIC_CASES = [
    # ---- judgment: 决策/事实陈述 ----
    ("Helper 生产端口是 8001", "judgment"),
    ("Helper 生产端口是 8009", "judgment"),
    ("我决定把首页风险章节前置", "judgment"),
    ("结论是放在末页效果更好", "judgment"),
    ("我们认为这次重构要分两期", "judgment"),
    ("已经确认 Wave 回调端口固定 8009", "judgment"),
    ("Athenai 的地址是 https://athenai.mihoyo.com", "judgment"),

    # ---- ask: 句尾问号 ----
    ("Helper 生产端口是多少?", "ask"),
    ("Helper 生产端口是多少?", "ask"),

    # ---- ask: 句尾语气助词 ----
    ("Helper 端口是 8001 吗?", "ask"),
    ("这个方案行不?", "ask"),
    ("可以这样改吗", "ask"),
    ("是不是该重启服务", "ask"),

    # ---- ask: 显式疑问词开头(短句) ----
    ("如何配置 nginx", "ask"),
    ("怎么部署 helper", "ask"),
    ("为什么要选 8009", "ask"),
    ("什么是 superseded", "ask"),
    ("哪个模型在跑 ask", "ask"),
    ("请问 Helper 怎么部署", "ask"),
    ("想问下 Athenai 的限流策略", "ask"),

    # ---- schedule_* ----
    ("取消 #3", "schedule_cancel"),
    ("取消 #99", "schedule_cancel"),
    ("我的定时任务有哪些", "schedule_list"),
    ("当前定时任务有哪些", "schedule_list"),
    ("列出我的定时任务", "schedule_list"),

    # ---- 边界: 长陈述句含问号 — 应判 ask(句尾问号优先) ----
    ("这个方案我们已经讨论过几轮,各种边界都覆盖到了,真的可以这样上线吗?", "ask"),
]

# 这些 case 必须走 LLM 兜底(启发式无信号)
LLM_FALLBACK_CASES = [
    "你好",                      # other(LLM 该判)— 不命中任何启发式
    "刚才那个 issue 怎么样了",      # 含"怎么样"但不是开头 — 不命中
]


@pytest.mark.parametrize("text,expected", HEURISTIC_CASES)
def test_heuristic_matches(monkeypatch, text, expected):
    import helper.im.intent as I

    def _no_llm(*args, **kwargs):
        raise AssertionError(f"LLM was called for text={text!r} (heuristic should have hit)")

    monkeypatch.setattr(I, "run", _no_llm)
    assert I.classify(text) == expected, f"text={text!r}"


def test_heuristic_does_not_misfire_on_ambiguous_short_chitchat(monkeypatch):
    """'你好' 这种闲聊应走 LLM 兜底,不该被启发式误命中。"""
    import helper.im.intent as I

    called = {"n": 0}

    def _stub_llm(*args, **kwargs):
        called["n"] += 1
        return "other"

    monkeypatch.setattr(I, "run", _stub_llm)
    result = I.classify("你好")
    assert called["n"] == 1, "启发式不该命中 '你好',应该走 LLM"
    assert result == "other"
