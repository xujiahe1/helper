"""ProseMirror JSON → 纯 markdown/text 渲染。

KM 协同文档(`doc_type=document`)的 `info.content` 字段是 ProseMirror 文档树
JSON 字符串(根 {"type":"doc","content":[...]}),直接当 markdown 喂给 L1
LLM 等于喂一坨结构噪音(node-id / tracked-author / mark-tracked / ...),抽不出
任何东西。

这里实现一个最小可用的渲染器:覆盖文档里出现频率高的节点(heading / paragraph
/ list / blockquote / table / image / video / code / card / mention / embed),
未识别节点 fallback 取子 inner — 不会丢内容,顶多丢格式。

设计原则:
- 输入是完整的 ProseMirror 树(已 json.loads 过的 dict)
- 输出是 markdown 风格纯文本,L1 LLM 当人话读
- 渲染失败(树结构异常)抛 ValueError,调用方自行决定 fallback
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def _render(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    t = node.get("type", "")
    children = node.get("content") or []
    inner = "".join(_render(c) for c in children)

    if t == "text":
        return node.get("text") or ""
    if t == "heading":
        level = (node.get("attrs") or {}).get("level") or 2
        try:
            level = max(1, min(6, int(level)))
        except (TypeError, ValueError):
            level = 2
        return "\n" + "#" * level + " " + inner.strip() + "\n"
    if t == "paragraph":
        return inner.strip() + "\n"
    if t == "blockquote":
        return "> " + inner.strip() + "\n"
    if t == "bullet_list" or t == "ordered_list":
        return inner
    if t == "list_item":
        return "- " + inner.strip() + "\n"
    if t == "code_block":
        return "\n```\n" + inner + "\n```\n"
    if t == "hard_break":
        return "\n"
    if t == "horizontal_rule":
        return "\n---\n"
    if t == "table":
        return "\n" + inner + "\n"
    if t == "table_row":
        return inner.rstrip(" |") + "\n"
    if t == "table_cell" or t == "table_header":
        return inner.strip() + " | "
    if t == "image":
        attrs = node.get("attrs") or {}
        alt = (attrs.get("alt") or attrs.get("title") or "").strip()
        return f"[图片{(' ' + alt) if alt else ''}]"
    if t == "video":
        return "[视频]"
    if t == "card" or t == "embed":
        return inner if inner.strip() else "[嵌入]"
    if t == "mention":
        attrs = node.get("attrs") or {}
        name = (attrs.get("name") or attrs.get("display_name") or "").strip()
        return f"@{name}" if name else "[@用户]"
    if t == "doc":
        return inner
    return inner


def render_prosemirror(content: str | dict) -> str:
    """ProseMirror JSON(字符串或已 parse 的 dict)→ markdown 纯文本。

    - 输入字符串则 json.loads;parse 失败抛 ValueError(调用方决定回退)
    - 根节点不是 dict 抛 ValueError
    - 多余空白行折叠成单个换行
    """
    if isinstance(content, str):
        try:
            doc = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"not a valid JSON: {e}") from e
    elif isinstance(content, dict):
        doc = content
    else:
        raise ValueError(f"unsupported input type: {type(content).__name__}")

    if not isinstance(doc, dict):
        raise ValueError(f"prosemirror root must be dict, got {type(doc).__name__}")

    text = _render(doc)
    # 多个连续空行 → 单个,首尾 strip
    lines = [ln.rstrip() for ln in text.split("\n")]
    out_lines: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if blank:
                continue
            blank = True
            out_lines.append("")
        else:
            blank = False
            out_lines.append(ln)
    return "\n".join(out_lines).strip()


def looks_like_prosemirror(content: str) -> bool:
    """快速判断字符串看起来像 ProseMirror tree(以 `{"type":"doc"` 开头)。

    用于 km_ingest 在 doc_type=document 时决定走渲染还是当 markdown。
    """
    if not content:
        return False
    s = content.lstrip()
    return s.startswith('{"type":"doc"') or s.startswith("{'type':'doc'")
