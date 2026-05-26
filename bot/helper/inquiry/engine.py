"""追问引擎 — yaml 策略 → 触发条件 → 命中即出问题。

DSL keys (when):
  scene_empty        bool — l1.scene 是否为空
  signals_count      int  — 必须等于 N
  signals_count_min  int  — >= N
  tradeoffs_count    int  — 必须等于 N
  tradeoffs_count_min int — >= N
  choice_empty       bool
  rationale_empty    bool
  contains           list[str] — raw 文本包含任一关键词
  not_contains       list[str] — raw 文本不包含任一关键词
  contains_counter   bool — 是否含"除非/但/反例"等反例标记
  hedge_count_min    int  — hedge 词("可能/或许/也许")出现 ≥ N
  not_contains_numbers bool — 文本不含阿拉伯数字
  source_type        str
  first_of_week      bool — 该作者本周首条
  has_similar_spec   bool — bundle 中存在相似 spec
  signals_too_abstract bool — 任一 signal 长度 ≤ 4 字
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from helper.config import get_settings
from helper.storage import session
from helper.storage.models import InquiryLog, L1Result, RawInput

log = logging.getLogger(__name__)

INQUIRY_RELPATH = Path("meta") / "policies" / "inquiry_strategies.yaml"
_DEFAULT_PACKAGE = "helper.policy.defaults"
_DEFAULT_FILE = "inquiry_strategies.yaml"


@dataclass
class InquiryHit:
    raw_id: int
    strategy_id: str
    question: str


def load_strategies() -> list[dict[str, Any]]:
    """从 spec repo 读策略;不存在则用打包默认。"""
    s = get_settings()
    f = s.helper_spec_git_dir / INQUIRY_RELPATH
    if f.exists():
        text = f.read_text(encoding="utf-8")
    else:
        text = (files(_DEFAULT_PACKAGE) / _DEFAULT_FILE).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    strategies = data.get("strategies", [])
    return strategies if isinstance(strategies, list) else []


_HEDGE_RE = re.compile(r"(可能|或许|也许|不确定|大概|应该|似乎|好像)")
_COUNTER_RE = re.compile(r"(除非|但是|不过|反例|例外|失效)")
_DIGIT_RE = re.compile(r"\d")


def _eval_condition(when: dict[str, Any], raw: RawInput, l1: L1Result | None, ctx: dict[str, Any]) -> bool:
    """全部条件必须满足才算命中。"""
    text = raw.content_text or ""
    signals = json.loads(l1.signals_json or "[]") if l1 else []
    tradeoffs = json.loads(l1.tradeoffs_json or "[]") if l1 else []

    for k, v in when.items():
        if k == "scene_empty":
            if bool(v) != (not (l1 and l1.scene)):
                return False
        elif k == "choice_empty":
            if bool(v) != (not (l1 and l1.choice)):
                return False
        elif k == "rationale_empty":
            if bool(v) != (not (l1 and l1.rationale)):
                return False
        elif k == "signals_count":
            if len(signals) != int(v):
                return False
        elif k == "signals_count_min":
            if len(signals) < int(v):
                return False
        elif k == "tradeoffs_count":
            if len(tradeoffs) != int(v):
                return False
        elif k == "tradeoffs_count_min":
            if len(tradeoffs) < int(v):
                return False
        elif k == "contains":
            if not isinstance(v, list) or not any(kw in text for kw in v):
                return False
        elif k == "not_contains":
            if isinstance(v, list) and any(kw in text for kw in v):
                return False
        elif k == "contains_counter":
            if bool(v) != bool(_COUNTER_RE.search(text)):
                return False
        elif k == "hedge_count_min":
            if len(_HEDGE_RE.findall(text)) < int(v):
                return False
        elif k == "not_contains_numbers":
            if bool(v) != (not _DIGIT_RE.search(text)):
                return False
        elif k == "source_type":
            if raw.source_type != v:
                return False
        elif k == "first_of_week":
            if bool(v) != ctx.get("first_of_week", False):
                return False
        elif k == "has_similar_spec":
            if bool(v) != ctx.get("has_similar_spec", False):
                return False
        elif k == "signals_too_abstract":
            abstract = any(len(str(sig)) <= 4 for sig in signals)
            if bool(v) != abstract:
                return False
    return True


def _format_question(template: str, raw: RawInput, l1: L1Result | None, ctx: dict[str, Any]) -> str:
    signals = json.loads(l1.signals_json or "[]") if l1 else []
    repls = {
        "{scene}": (l1.scene if l1 else "") or "(场景)",
        "{choice}": (l1.choice if l1 else "") or "(选择)",
        "{rationale}": (l1.rationale if l1 else "") or "",
        "{first_signal}": signals[0] if signals else "",
        "{similar_spec_title}": ctx.get("similar_spec_title", ""),
    }
    out = template
    for k, v in repls.items():
        out = out.replace(k, v)
    return out


def _max_per_day_ok(strategy_id: str, author: str, max_per_day: int) -> bool:
    if not author:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with session() as s:
        n = len(s.execute(
            select(InquiryLog.id)
            .join(RawInput, InquiryLog.raw_id == RawInput.id)
            .where(InquiryLog.strategy_id == strategy_id)
            .where(InquiryLog.created_at >= cutoff)
            .where(RawInput.author_domain == author)
        ).scalars().all())
    return n < max_per_day


def _is_first_of_week(author: str, raw_id: int) -> bool:
    if not author:
        return False
    monday = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    with session() as s:
        prev = s.execute(
            select(RawInput.id)
            .where(RawInput.author_domain == author)
            .where(RawInput.created_at >= monday)
            .where(RawInput.id != raw_id)
            .limit(1)
        ).scalars().first()
    return prev is None


def _has_similar_spec(text: str) -> tuple[bool, str]:
    """简易关键词命中 bundle 中 spec.title。返 (是否命中, 命中标题)。"""
    try:
        from helper.compiler import load_bundle
        bundle = load_bundle()
    except Exception:  # noqa: BLE001
        return False, ""
    text_lc = text.lower()
    for sp in bundle.get("specs", []):
        title = str(sp.get("title", "")).strip()
        if not title:
            continue
        # 标题中任意 ≥3 字 token 出现在 text 视为命中
        for tok in re.findall(r"[\w一-鿿]{3,}", title):
            if tok.lower() in text_lc:
                return True, title
    return False, ""


def evaluate_for_raw(raw_id: int) -> list[InquiryHit]:
    """对一条 raw 评估所有策略,命中的写 inquiry_log + 返回触发列表。"""
    strategies = load_strategies()
    if not strategies:
        return []

    with session() as s:
        raw = s.get(RawInput, raw_id)
        l1 = s.get(L1Result, raw_id)
        if raw is None:
            return []
        author = raw.author_domain
        text = raw.content_text or ""

    has_sim, sim_title = _has_similar_spec(text)
    ctx: dict[str, Any] = {
        "first_of_week": _is_first_of_week(author, raw_id),
        "has_similar_spec": has_sim,
        "similar_spec_title": sim_title,
    }

    hits: list[InquiryHit] = []
    for strat in strategies:
        sid = str(strat.get("id", ""))
        when = strat.get("when") or {}
        question_tpl = str(strat.get("question", ""))
        max_per_day = int(strat.get("max_per_day", 3))
        if not sid or not question_tpl:
            continue
        if not _eval_condition(when, raw, l1, ctx):
            continue
        if not _max_per_day_ok(sid, author, max_per_day):
            continue
        question = _format_question(question_tpl, raw, l1, ctx)
        with session() as s:
            row = InquiryLog(raw_id=raw_id, strategy_id=sid, question=question)
            s.add(row)
            s.commit()
        hits.append(InquiryHit(raw_id=raw_id, strategy_id=sid, question=question))
    return hits


def record_answer(inquiry_id: int, answer_raw_id: int) -> None:
    """用户回答了某次追问 — 把回答的 raw_id 关联回去。"""
    with session() as s:
        row = s.get(InquiryLog, inquiry_id)
        if row is None:
            return
        row.answer_raw_id = answer_raw_id
        s.commit()


def mark_hit(inquiry_id: int, hit: str) -> None:
    """标记本次追问是否答到点(yes/no/unknown)。M2 末用 LLM 自动 judge,M1 手动。"""
    if hit not in ("yes", "no", "unknown"):
        raise ValueError(f"hit must be yes/no/unknown, got {hit!r}")
    with session() as s:
        row = s.get(InquiryLog, inquiry_id)
        if row is None:
            return
        row.hit = hit
        s.commit()
