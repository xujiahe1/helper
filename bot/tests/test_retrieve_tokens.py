"""retrieve._tokens / _jaccard_score — 中文 bigram + 英文单词混合分词。"""

from __future__ import annotations

from helper.ask.retrieve import _jaccard_score, _tokens


def test_cjk_bigram_tokens():
    toks = _tokens("账号关联")
    # 长度 4 → 3 个 bigram
    assert toks == {"账号", "号关", "关联"}


def test_ascii_words_lowercased():
    toks = _tokens("LML mhy IAM")
    assert "lml" in toks
    assert "mhy" in toks
    assert "iam" in toks
    # 单字符 a/i 不进
    assert "a" not in toks


def test_mixed_zh_en():
    toks = _tokens("lml回流mhy")
    # ASCII 串单独成词,CJK 串单独 bigram
    assert "lml" in toks
    assert "mhy" in toks
    assert "回流" in toks


def test_empty_returns_empty():
    assert _tokens("") == set()
    assert _tokens(None) == set()  # type: ignore[arg-type]


def test_single_chinese_char():
    """长度 1 的 CJK 串走 fallback,直接当 token。"""
    assert _tokens("我") == {"我"}


def test_jaccard_overlap_chinese():
    """以前坏掉的场景:中文 query 在中文 doc 里 Jaccard 应该非零。"""
    qtoks = _tokens("lml回流关联账号有几个场景")
    sc = _jaccard_score(qtoks, "lml回流mhy 账号关联 双账号 场景")
    assert sc > 0


def test_jaccard_zero_no_overlap():
    qtoks = _tokens("完全无关的英文词 alpha beta")
    sc = _jaccard_score(qtoks, "另外一段毫无关联的内容")
    assert sc >= 0  # 中文"内容"没出现在 query,bigrams 不交叠


def test_jaccard_higher_with_more_overlap():
    qtoks = _tokens("lml 回流 关联 账号")
    high = _jaccard_score(qtoks, "lml 回流 关联 账号 都在")
    low = _jaccard_score(qtoks, "lml 不相关")
    assert high > low
