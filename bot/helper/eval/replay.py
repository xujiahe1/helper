"""Replay implementation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from helper.ask import ask
from helper.compiler import current_bundle_version
from helper.llm import run
from helper.storage import session
from helper.storage.models import AskAnswer

log = logging.getLogger(__name__)


@dataclass
class ReplayItem:
    question: str
    original_answer: str
    original_version: str
    original_confidence: str
    new_answer: str = ""
    new_version: str = ""
    new_confidence: str = ""
    new_citations: list[dict[str, Any]] = field(default_factory=list)


def replay_one(question: str, *, asker: str = "replay") -> dict[str, Any]:
    """单题用当前 bundle 重跑 ask。"""
    ans = ask(question, asker_domain=asker)
    return {
        "answer": ans.answer,
        "confidence": ans.confidence,
        "citations": ans.citations,
        "bundle_version": ans.bundle_version,
    }


def replay_all(*, limit: int = 100, since_id: int | None = None) -> list[ReplayItem]:
    """把 ask_answers 表里历史问题全部 replay。"""
    items: list[ReplayItem] = []
    with session() as s:
        q = select(AskAnswer).order_by(AskAnswer.id.asc()).limit(limit)
        if since_id is not None:
            q = q.where(AskAnswer.id > since_id)
        rows = s.execute(q).scalars().all()
        for r in rows:
            items.append(ReplayItem(
                question=r.question,
                original_answer=r.answer,
                original_version=r.spec_bundle_version,
                original_confidence=r.confidence,
            ))

    new_v = current_bundle_version()
    for it in items:
        try:
            ans = ask(it.question, asker_domain="replay")
            it.new_answer = ans.answer
            it.new_confidence = ans.confidence
            it.new_citations = ans.citations
            it.new_version = ans.bundle_version
        except Exception as e:  # noqa: BLE001
            log.warning("replay failed q=%r: %s", it.question[:60], e)
            it.new_answer = f"(replay error: {type(e).__name__})"
            it.new_version = new_v
    return items


JUDGE_PROMPT = """对同一问题,两个版本的回答,哪个更好?

问题:
{question}

A 版本({a_v}):
{a}

B 版本({b_v}):
{b}

输出 JSON:
{{"winner": "A | B | tie", "reason": "一句话"}}

判断标准: 准确性 > 引用充分性 > 不确定性自标 > 简洁。
只输出 JSON。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def judge_better(item: ReplayItem) -> dict[str, Any]:
    """LLM judge 哪个回答更好。"""
    if not item.new_answer or not item.original_answer:
        return {"winner": "tie", "reason": "缺一边"}
    prompt = JUDGE_PROMPT.format(
        question=item.question,
        a_v=item.original_version or "old",
        a=item.original_answer,
        b_v=item.new_version or "new",
        b=item.new_answer,
    )
    try:
        reply = run("conflict_judge", user=prompt, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("judge LLM failed: %s", e)
        return {"winner": "tie", "reason": f"judge error: {e}"}
    data = _parse_json(reply) or {}
    winner = str(data.get("winner", "tie")).upper()
    if winner not in ("A", "B", "TIE"):
        winner = "TIE"
    return {"winner": winner.lower(), "reason": str(data.get("reason", ""))}


def compare_versions(*, limit: int = 100) -> dict[str, Any]:
    """全量 replay + judge,返回汇总。"""
    items = replay_all(limit=limit)
    judgements = []
    score = {"a": 0, "b": 0, "tie": 0}
    for it in items:
        j = judge_better(it)
        judgements.append({
            "question": it.question[:80],
            "old_version": it.original_version,
            "new_version": it.new_version,
            "old_answer": it.original_answer[:200],
            "new_answer": it.new_answer[:200],
            "winner": j["winner"],
            "reason": j["reason"],
        })
        score[j["winner"]] = score.get(j["winner"], 0) + 1
    return {"total": len(items), "score": score, "items": judgements}
