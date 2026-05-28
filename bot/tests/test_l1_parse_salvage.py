"""L1 JSON 解析容错 — 截断 / 局部坏数据时尽量挽救。"""

from __future__ import annotations

from helper.ingest.l1_structure import _parse_json_array


def test_parse_clean_array():
    arr = _parse_json_array('[{"type": "concept", "name": "A"}]')
    assert arr == [{"type": "concept", "name": "A"}]


def test_parse_with_fence():
    arr = _parse_json_array('```json\n[{"type":"fact","subject":"X"}]\n```')
    assert arr == [{"type": "fact", "subject": "X"}]


def test_parse_empty_array():
    assert _parse_json_array("[]") == []


def test_parse_no_array_returns_none():
    assert _parse_json_array("just plain text") is None


def test_parse_truncated_array_salvages_complete_objects():
    """LLM 输出被 max_tokens 截断在一个 obj 中间 — 之前的完整 obj 应该都救出来。"""
    text = """[
  {"type": "concept", "name": "A", "description": "first"},
  {"type": "concept", "name": "B", "description": "second"},
  {"type": "concept", "name": "C", "descrip"""  # 截断
    arr = _parse_json_array(text)
    assert arr is not None
    assert len(arr) == 2
    assert arr[0]["name"] == "A"
    assert arr[1]["name"] == "B"


def test_parse_one_bad_object_skipped():
    """中间一个 obj 字符串没转义引号坏掉,前后好的应该都进来。"""
    text = """[
  {"type": "concept", "name": "good1"},
  {"type": "concept", "name": "bad" extra },
  {"type": "concept", "name": "good2"}
]"""
    arr = _parse_json_array(text)
    assert arr is not None
    names = [o.get("name") for o in arr]
    assert "good1" in names
    assert "good2" in names


def test_parse_handles_string_with_braces():
    """字符串字面量里的 { 不该把对象边界算错。"""
    text = '[{"type":"fact","subject":"含 { 和 } 的字符串","object":"x"}]'
    arr = _parse_json_array(text)
    assert arr is not None
    assert len(arr) == 1
    assert arr[0]["subject"] == "含 { 和 } 的字符串"


def test_parse_fenced_truncated():
    """fence + 截断。"""
    text = """```json
[
  {"type": "case", "scene": "S", "what_happened": "W"},
  {"type": "case", "scene": "S2", "what_hap"""
    arr = _parse_json_array(text)
    assert arr is not None
    assert len(arr) == 1
    assert arr[0]["scene"] == "S"
