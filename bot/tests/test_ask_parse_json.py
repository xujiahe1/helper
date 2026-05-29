"""runtime._parse_json — 容忍 LLM 长回答里的真实换行(strict=False)。

回归场景:LLM 在 answer 字段里输出多行长文本,没把 \\n 转义掉,
默认 json.loads(strict=True) 会拒绝,导致 answer 整段 JSON 字符串被原样
塞回 wave、confidence 退化成 unknown、citations 丢失。
"""

from __future__ import annotations

from helper.ask.runtime import _parse_json


def test_parse_json_with_real_newlines_in_string():
    raw = (
        '{"answer":"根据检索结果:\n\n1. 场景一\n2. 场景二",'
        '"confidence":"medium","citations":[{"type":"raw","ref":"20"}]}'
    )
    out = _parse_json(raw)
    assert out is not None
    assert "场景一" in out["answer"]
    assert out["confidence"] == "medium"
    assert out["citations"] == [{"type": "raw", "ref": "20"}]


def test_parse_json_with_tabs_in_string():
    raw = '{"answer":"a\tb\tc","confidence":"low","citations":[]}'
    out = _parse_json(raw)
    assert out is not None
    assert out["answer"] == "a\tb\tc"


def test_parse_json_in_fenced_block():
    raw = '```json\n{"answer":"X\n多行","confidence":"high","citations":[]}\n```'
    out = _parse_json(raw)
    assert out is not None
    assert "多行" in out["answer"]


def test_parse_json_garbage_returns_none():
    assert _parse_json("not json at all") is None
    assert _parse_json("") is None


def test_parse_json_non_dict_returns_none():
    """顶层不是 dict 的合法 JSON(数组、字符串)应返 None。"""
    assert _parse_json('["a","b"]') is None
