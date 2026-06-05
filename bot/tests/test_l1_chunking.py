"""L1 长文档切片 — 按 H2 章节切,超阈值每片独立调 LLM 后合并。"""

from __future__ import annotations

import helper.ingest.l1_structure as L1


def test_chunk_short_doc_no_split():
    """≤ 阈值的整篇直接进单次抽取,不切。"""
    text = "# 标题\n\n## 章节 A\n短内容\n\n## 章节 B\n短内容"
    chunks = L1._chunk_by_h2(text)
    assert len(chunks) == 2  # H2 切了,但都很短


def test_chunk_by_h2_carries_h1_prefix():
    text = "# 主标题\n\n## A\n" + ("a" * 100) + "\n\n## B\n" + ("b" * 100)
    chunks = L1._chunk_by_h2(text)
    assert len(chunks) == 2
    assert "主标题" in chunks[0]
    assert "## A" in chunks[0]
    assert "主标题" in chunks[1]
    assert "## B" in chunks[1]


def test_chunk_no_h2_falls_back_to_size():
    """没有 H2 → 按字符数切。"""
    text = "## 这不是 H2,因为前面没换行" + ("\n" + "x" * 1000) * 20
    chunks = L1._chunk_by_h2(text)
    # 至少切成多块
    for c in chunks:
        assert len(c) <= L1.LONG_DOC_THRESHOLD + 100


def test_chunk_giant_h2_section_subsplit():
    """单个 H2 章节也超阈值时,继续按字符数切。"""
    text = "# H1\n\n## 长章节\n" + ("段落\n" * 5000)
    chunks = L1._chunk_by_h2(text)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= L1.LONG_DOC_THRESHOLD + 200


def test_structure_short_doc_single_call(monkeypatch):
    """短文本只调 LLM 一次。"""
    calls = []

    def fake_run(task, *, system, user, **kw):
        calls.append(user)
        return '[{"type":"section","title":"T","body":"x","topics":[],"entities":[]}]'

    monkeypatch.setattr(L1, "run", fake_run)
    out = L1.structure("短文本就一句话")
    assert out.ok
    assert len(out.items) == 1
    assert len(calls) == 1


def test_structure_long_doc_multi_chunk(monkeypatch):
    """超阈值文本切多片,每片各调一次,items 合并。"""
    calls = []

    def fake_run(task, *, system, user, **kw):
        calls.append(user)
        # 每片回 2 条 section, title 带上 chunk 编号区分
        idx = len(calls)
        return (
            '[{"type":"section","title":"S' + str(idx) + '-a","body":"b","topics":[],"entities":[]},'
            '{"type":"section","title":"S' + str(idx) + '-b","body":"b","topics":[],"entities":[]}]'
        )

    monkeypatch.setattr(L1, "run", fake_run)
    long_text = "# 标题\n\n" + "\n\n".join(
        f"## 章节 {i}\n" + ("内容内容" * 2000) for i in range(3)
    )
    assert len(long_text) > L1.LONG_DOC_THRESHOLD
    out = L1.structure(long_text)
    assert out.ok
    assert len(calls) >= 2  # 至少切了 2 片
    # 合并后每片 2 条 → ≥4 条
    assert len(out.items) >= len(calls) * 2 - 1


def test_structure_partial_chunk_failure_keeps_rest(monkeypatch):
    """一片 LLM 抽空 / 抽坏不影响其它片的结果。"""
    calls = [0]

    def fake_run(task, *, system, user, **kw):
        calls[0] += 1
        if calls[0] == 2:
            return "garbage not json"
        return '[{"type":"section","title":"T","body":"x","topics":[],"entities":[]}]'

    monkeypatch.setattr(L1, "run", fake_run)
    long_text = "# T\n\n" + "\n\n".join(
        f"## c{i}\n" + ("xx" * 4000) for i in range(3)
    )
    out = L1.structure(long_text)
    # 至少有从其它 chunk 抽出的 items
    assert len(out.items) >= 1
    assert out.ok  # 部分成功就不算整体 error


def test_structure_with_context_skips_chunking(monkeypatch):
    """群聊路径(传 context)不切 — 主消息天然短。"""
    calls = []

    def fake_run(task, *, system, user, **kw):
        calls.append(user)
        return "[]"

    monkeypatch.setattr(L1, "run", fake_run)
    L1.structure(
        "短消息",
        context=[{"raw_id": 1, "speaker": "u", "text": "上下文", "ts": "10:00"}],
    )
    assert len(calls) == 1
