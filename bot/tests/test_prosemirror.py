"""ProseMirror JSON → 纯文本渲染。"""

from __future__ import annotations

import json

import pytest

from helper.im.prosemirror import (
    looks_like_prosemirror,
    render_prosemirror,
)


def _doc(*content):
    return {"type": "doc", "content": list(content)}


def _h(level, text):
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _p(*texts):
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": t} for t in texts],
    }


def test_looks_like_prosemirror_true():
    assert looks_like_prosemirror('{"type":"doc","content":[]}')
    assert looks_like_prosemirror('  {"type":"doc","content":[]}  ')


def test_looks_like_prosemirror_false():
    assert not looks_like_prosemirror("# 普通 markdown\n正文")
    assert not looks_like_prosemirror("")
    assert not looks_like_prosemirror('{"foo":"bar"}')


def test_render_simple_doc():
    doc = _doc(_h(1, "标题"), _p("第一段"), _p("第二段"))
    out = render_prosemirror(doc)
    assert "# 标题" in out
    assert "第一段" in out
    assert "第二段" in out


def test_render_accepts_string_input():
    s = json.dumps(_doc(_h(2, "二级"), _p("正文")))
    out = render_prosemirror(s)
    assert "## 二级" in out
    assert "正文" in out


def test_render_invalid_json_raises():
    with pytest.raises(ValueError):
        render_prosemirror("not json")


def test_render_invalid_root_raises():
    with pytest.raises(ValueError):
        render_prosemirror("[]")


def test_render_list():
    doc = _doc({
        "type": "bullet_list",
        "content": [
            {"type": "list_item", "content": [_p("项 A")]},
            {"type": "list_item", "content": [_p("项 B")]},
        ],
    })
    out = render_prosemirror(doc)
    assert "- 项 A" in out
    assert "- 项 B" in out


def test_render_table():
    doc = _doc({
        "type": "table",
        "content": [
            {
                "type": "table_row",
                "content": [
                    {"type": "table_cell", "content": [_p("姓名")]},
                    {"type": "table_cell", "content": [_p("部门")]},
                ],
            },
            {
                "type": "table_row",
                "content": [
                    {"type": "table_cell", "content": [_p("张三")]},
                    {"type": "table_cell", "content": [_p("研发")]},
                ],
            },
        ],
    })
    out = render_prosemirror(doc)
    assert "姓名" in out and "部门" in out
    assert "张三" in out and "研发" in out


def test_render_image_with_alt():
    doc = _doc({
        "type": "image",
        "attrs": {"alt": "架构图"},
    })
    out = render_prosemirror(doc)
    assert "[图片 架构图]" in out


def test_render_blockquote():
    doc = _doc({
        "type": "blockquote",
        "content": [_p("引用内容")],
    })
    out = render_prosemirror(doc)
    assert "> 引用内容" in out


def test_render_strips_noise_attrs():
    """real-world ProseMirror node 带一堆 tracked-author / mark-tracked / node-id
    噪音 attrs,渲染只取 text。"""
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {
                    "node-id": "abc123",
                    "tracked-author": "alice",
                    "mark-tracked": "0|alice|unset",
                    "level": 2,
                },
                "content": [
                    {
                        "type": "text",
                        "text": "需求背景",
                        "marks": [
                            {"type": "track", "attrs": {"type": "mark-tracked"}},
                        ],
                    }
                ],
            },
        ],
    }
    out = render_prosemirror(doc)
    assert "## 需求背景" in out
    assert "node-id" not in out
    assert "tracked-author" not in out


def test_render_unknown_node_falls_back_to_inner():
    doc = _doc({
        "type": "未知节点类型",
        "content": [_p("但里面有正文")],
    })
    out = render_prosemirror(doc)
    assert "但里面有正文" in out


def test_render_collapses_blank_lines():
    doc = _doc(_p("A"), _p(""), _p(""), _p("B"))
    out = render_prosemirror(doc)
    # 多个空行折叠
    assert "A\n\nB" in out or "A\n\n\nB" not in out
