"""FTS5 + jieba 词面召回 — 写入 / 召回 / supersede 同步删除 / 1k 行 perf 抽样。

针对 M7 retrieve 索引化:1000 篇规模下 raw / candidate 全扫不能再走 Python Jaccard。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone


def test_tokenize_chinese_segments_with_jieba():
    """jieba 切词:领域名词应整段命中,不只是 2-gram 拆字。"""
    from helper.storage.fts import tokenize

    out = tokenize("Helper 生产端口是 8009")
    # 包含整词,不是字符级
    assert "生产" in out or "端口" in out or "生产端口" in out
    # 数字和英文也保留
    assert "8009" in out
    assert "helper" in out.lower() or "Helper" in out


def test_tokenize_handles_empty_and_special_chars():
    from helper.storage.fts import tokenize

    assert tokenize("") == ""
    # fts5 元字符不能漏到查询里 — 切完应该是普通空格分隔串
    out = tokenize("a-b-c (d) \"e\"")
    assert "(" not in out
    assert "\"" not in out


def test_search_basic_recall(db, settings):
    """upsert 一条 → search 同关键词能召到。"""
    from helper.storage import fts, session

    with session() as s:
        fts.upsert(s, kind="section", ref="1:0", content="Helper 生产端口是 8009")
        s.commit()

    with session() as s:
        hits = fts.search(s, query="生产端口", top_k=10)
    refs = {(k, r) for k, r, _ in hits}
    assert ("section", "1:0") in refs


def test_search_kind_filter(db, settings):
    """kinds=['raw'] 只返 raw,不返其它。"""
    from helper.storage import fts, session

    with session() as s:
        fts.upsert(s, kind="raw", ref="100", content="生产端口")
        fts.upsert(s, kind="section", ref="2:0", content="生产端口")
        s.commit()

    with session() as s:
        hits = fts.search(s, query="生产端口", top_k=10, kinds=["raw"])
    kinds_seen = {k for k, _, _ in hits}
    assert kinds_seen == {"raw"}


def test_upsert_replaces_old_text(db, settings):
    """同 (kind, ref) upsert 第二次 → 旧文本独有的关键词搜不到,新文本能搜到。"""
    from helper.storage import fts, session

    # 用完全不重叠的词:旧"芒果橘子"vs 新"葡萄苹果",避免 OR 召回串台
    with session() as s:
        fts.upsert(s, kind="section", ref="3:0", content="芒果 橘子 9000")
        s.commit()
    with session() as s:
        fts.upsert(s, kind="section", ref="3:0", content="葡萄 苹果 8009")
        s.commit()

    with session() as s:
        hits_old = fts.search(s, query="芒果", top_k=10)
        hits_new = fts.search(s, query="葡萄", top_k=10)
    assert ("section", "3:0") not in {(k, r) for k, r, _ in hits_old}
    assert ("section", "3:0") in {(k, r) for k, r, _ in hits_new}


def test_delete_removes_from_index(db, settings):
    """fts.delete 后,该 ref 不再被召回。"""
    from helper.storage import fts, session

    with session() as s:
        fts.upsert(s, kind="section", ref="4:0", content="发版前周一不发版本")
        s.commit()
    with session() as s:
        fts.delete(s, kind="section", ref="4:0")
        s.commit()
    with session() as s:
        hits = fts.search(s, query="周一不发版本", top_k=10)
    assert ("section", "4:0") not in {(k, r) for k, r, _ in hits}


# ---------- supersede 自动清场(detector 路径) ----------

def test_supersede_target_clears_fts_and_vector(db, settings):
    """conflict._supersede_target 给候选打 superseded_at 时,同步清 fts/vec。"""
    from helper.conflict.detector import _supersede_target
    from helper.storage import fts, session
    from helper.storage.models import SpecCandidate

    with session() as s:
        s.add(SpecCandidate(
            slug="s-old", title="老规约", statement="Helper 端口是 8001",
        ))
    with session() as s:
        fts.index_spec(s, "s-old")
        s.commit()

    # 确认能搜到
    with session() as s:
        hits_before = fts.search(s, query="Helper 端口 8001", top_k=10)
    assert ("spec", "s-old") in {(k, r) for k, r, _ in hits_before}

    # 触发 supersede
    with session() as s:
        _supersede_target(s, "spec", "s-old", raw_id=99)
        s.commit()

    # fts 应该已经清掉
    with session() as s:
        hits_after = fts.search(s, query="Helper 端口 8001", top_k=10)
    assert ("spec", "s-old") not in {(k, r) for k, r, _ in hits_after}


# ---------- 中文召回:确认 jieba 切词比单字 2-gram 强 ----------

def test_chinese_domain_term_recalled(db, settings):
    """领域专有名词写入后,query 用同义改写形式能召回。"""
    from helper.storage import fts, session

    with session() as s:
        fts.upsert(s, kind="section", ref="iam:0",
                   content="加黑规则组 仅可配置主体不可见客体的规则组")
        s.commit()
    with session() as s:
        hits = fts.search(s, query="加黑规则", top_k=10)
    assert ("section", "iam:0") in {(k, r) for k, r, _ in hits}


# ---------- perf 抽样:1k 行 search 应该是毫秒级,而不是秒级 ----------

def test_perf_thousand_rows_under_500ms(db, settings):
    """灌 1000 条 fact 后,单次 search 应远低于 500ms。

    不是严格 perf 测,是为了挡住"FTS 没生效又退回全扫"的回归。
    """
    from helper.storage import fts, session

    # 灌库:1000 条不同主题的 fact,每条约 50 中文字符
    with session() as s:
        for i in range(1000):
            content = f"实体{i % 50} 谓词{i % 30} 对象{i} 描述 关于 测试 数据 第{i}条"
            fts.upsert(s, kind="section", ref=f"perf:{i}", content=content)
        s.commit()

    # 跑 5 次 search 取最大值,排除 jieba 冷启动
    with session() as s:
        fts.search(s, query="warmup", top_k=10)  # 暖一下 jieba

    durations = []
    with session() as s:
        for q in ("实体5 测试", "对象99 描述", "实体10 谓词20", "测试 数据", "第500条"):
            t0 = time.perf_counter()
            fts.search(s, query=q, top_k=10)
            durations.append(time.perf_counter() - t0)
    worst = max(durations)
    assert worst < 0.5, f"FTS 1k 行 search 最坏 {worst*1000:.0f}ms,超过 500ms 阈值"


def _utc_now():
    return datetime.now(timezone.utc)
