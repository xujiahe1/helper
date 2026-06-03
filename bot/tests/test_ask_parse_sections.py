"""runtime 输出解析 — 现在只剩 ROUTE 哨兵 + fence 剥离两件事。

历史: 早期是 JSON 解析(被未转义引号撞翻), 后来改 markdown 分段(## 答复/置信度/引用),
最新一版彻底删掉所有"输出脚手架": LLM 直接 plain 回答, 不再有段标题/置信度/引用。
本文件只保留 ROUTE 路由判定和 fence 剥离这两件 runtime 仍需的解析行为。
"""

from __future__ import annotations

from helper.ask.runtime import _parse_route, _strip_fence


def test_parse_route_first_line_sentinel():
    # 新格式: 只输 bot 名字, 由代码反查 app_id
    assert _parse_route("ROUTE: tachi") == "tachi"
    # fence 里也算
    assert _parse_route("```\nROUTE: tachi\n```") == "tachi"
    # 必须在最前面 — 写在答复正文中间不算路由
    assert _parse_route("## 答复\n哎\nROUTE: tachi") is None
    # 旧格式 'cli_xxx | name' 兼容: 丢弃 cli_, 只取 name
    assert _parse_route("ROUTE: cli_xxx | tachi") == "tachi"
    assert _parse_route("ROUTE: cli_aaa | tachi") == "tachi"
    # 旧格式裸 cli_xxx 没 name → 没法反查 → 失败
    assert _parse_route("ROUTE: cli_xxx") is None


def test_strip_fence_no_fence_passthrough():
    assert _strip_fence("hello\nworld") == "hello\nworld"


def test_strip_fence_unwraps_markdown_block():
    raw = "```markdown\n这是一条直接给用户的回复\n包含多行也 OK\n```"
    assert _strip_fence(raw) == "这是一条直接给用户的回复\n包含多行也 OK"
