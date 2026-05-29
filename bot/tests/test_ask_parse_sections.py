"""runtime 输出解析 — markdown 切片版(替换原 JSON 版)。

回归场景:LLM 在答复正文里输出多行长文本 / 引号 / 列表,
原 JSON 解析会被未转义引号撞翻、整段 raw JSON 被原样塞回 wave、
confidence 退化成 unknown、citations 丢失。改成段标题切片后这些都不再是问题。
"""

from __future__ import annotations

from helper.ask.runtime import _parse_citations, _parse_route, _parse_sections, _strip_fence


def test_parse_sections_three_blocks():
    raw = (
        "## 答复\n"
        '可见性在 IAM 管理后台配置, 路径: 组织管理→可见性规则。包含三套规则:\n'
        '1. 人见人 ("账号搜索")\n'
        '2. 人见组织 (含 "通讯录查看依赖")\n'
        '3. 人见通讯录\n\n'
        "## 置信度\nhigh\n\n"
        "## 引用\n"
        "- raw: 39\n"
        "- entity: 人见人规则\n"
    )
    secs = _parse_sections(raw)
    assert "可见性" in secs["答复"]
    assert '"账号搜索"' in secs["答复"]   # 引号原样保留
    assert "1. 人见人" in secs["答复"]    # 列表保留
    assert secs["置信度"] == "high"
    cits = _parse_citations(secs["引用"])
    assert cits == [
        {"type": "raw", "ref": "39"},
        {"type": "entity", "ref": "人见人规则"},
    ]


def test_parse_sections_handles_fenced_block():
    raw = (
        "```markdown\n"
        "## 答复\nX\n## 置信度\nlow\n## 引用\n\n"
        "```"
    )
    secs = _parse_sections(raw)
    assert secs["答复"] == "X"
    assert secs["置信度"] == "low"
    assert secs["引用"] == ""


def test_parse_route_first_line_sentinel():
    assert _parse_route("ROUTE: cli_xxx | tachi") == ("cli_xxx", "tachi")
    assert _parse_route("ROUTE: cli_xxx") == ("cli_xxx", "")
    # fence 里也算
    assert _parse_route("```\nROUTE: cli_a | b\n```") == ("cli_a", "b")
    # 必须在最前面 — 写在答复正文中间不算路由
    assert _parse_route("## 答复\n哎\nROUTE: cli_x") is None


def test_parse_citations_skips_garbage():
    block = (
        "- spec: rule_a\n"
        "随便一行\n"
        "- raw: 12\n"
        "* entity: 哥\n"
        "- bad-line-no-colon\n"
    )
    assert _parse_citations(block) == [
        {"type": "spec", "ref": "rule_a"},
        {"type": "raw", "ref": "12"},
        {"type": "entity", "ref": "哥"},
    ]


def test_parse_sections_garbage_returns_empty():
    """LLM 完全没按格式 → 返空 dict, ask runtime 兜底用整段。"""
    assert _parse_sections("我就是不按规矩输出 哈哈") == {}
    assert _parse_sections("") == {}


def test_strip_fence_no_fence_passthrough():
    assert _strip_fence("hello\nworld") == "hello\nworld"
