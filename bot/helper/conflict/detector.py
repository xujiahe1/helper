"""LLM judge 冲突 — 走 conflict_judge(claude-opus-4-7)。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from helper.ask.retrieve import retrieve_relevant
from helper.llm import run
from helper.storage import session
from helper.storage.models import ConflictLog, L1Result, RawInput

log = logging.getLogger(__name__)


@dataclass
class ConflictHit:
    raw_id: int
    spec_slug: str
    summary: str
    severity: str  # low / medium / high
    log_id: int | None = None


SYSTEM_PROMPT = """你是决策规约冲突 judge。给你:
1. 一条新输入(raw + L1 结构化)
2. 一条已沉淀的决策规约(spec)

判断它们是否冲突。输出 JSON:
{
  "verdict": "contradicts | refines | none",
  "summary": "一句话说明冲突点(verdict=none 时填空串)",
  "severity": "low | medium | high"
}

判断标准:
- contradicts: 在相同场景下,新输入的 choice 与 spec 的 statement 冲突,无法同时成立
- refines: 同方向,新输入是对 spec 的具体化/边界补充,不矛盾
- none: 不在同一场景 / 完全无关

severity:
- high: 直接颠覆,影响后续所有同场景决策
- medium: 部分冲突,有妥协空间
- low: 边缘冲突,可能只是表述差异

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
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _format_pair(raw: RawInput, l1: L1Result | None, spec_body: str, spec_title: str) -> str:
    parts = [
        "## 新输入(raw)",
        f"原文: {raw.content_text[:600]}",
    ]
    if l1 and not l1.error:
        parts += [
            f"场景: {l1.scene}",
            f"选择: {l1.choice}",
            f"原因: {l1.rationale}",
        ]
    parts += [
        "",
        f"## 已有 spec — {spec_title}",
        spec_body[:1200],
    ]
    return "\n".join(parts)


def detect_for_raw(raw_id: int, *, top_k_specs: int = 3) -> list[ConflictHit]:
    """对一条 raw 检索相关 spec → judge → 落冲突。返触发的列表。"""
    with session() as s:
        raw = s.get(RawInput, raw_id)
        l1 = s.get(L1Result, raw_id)
        if raw is None:
            return []

    text = (raw.content_text or "") + "\n" + (l1.scene if l1 else "") + "\n" + (l1.choice if l1 else "")
    hits = retrieve_relevant(text, top_k=top_k_specs)
    spec_hits = [h for h in hits if h.type == "spec"]
    if not spec_hits:
        return []

    out: list[ConflictHit] = []
    for sh in spec_hits:
        prompt = _format_pair(raw, l1, sh.body, sh.title)
        try:
            reply = run("conflict_judge", system=SYSTEM_PROMPT, user=prompt, temperature=0)
        except Exception as e:  # noqa: BLE001
            log.warning("conflict_judge LLM failed raw#%d spec=%s: %s", raw_id, sh.ref, e)
            continue
        data = _parse_json(reply) or {}
        verdict = str(data.get("verdict", "none")).lower()
        if verdict != "contradicts":
            continue
        summary = str(data.get("summary", "")).strip()
        severity = str(data.get("severity", "medium")).lower()
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        with session() as s:
            existing = s.execute(
                select(ConflictLog)
                .where(ConflictLog.raw_id == raw_id)
                .where(ConflictLog.spec_slug == sh.ref)
                .where(ConflictLog.resolution == "open")
            ).scalar_one_or_none()
            if existing is not None:
                # 同 raw + spec + 还 open → 不重复落
                out.append(ConflictHit(
                    raw_id=raw_id, spec_slug=sh.ref, summary=summary, severity=severity, log_id=existing.id,
                ))
                continue
            row = ConflictLog(
                raw_id=raw_id,
                spec_slug=sh.ref,
                summary=summary,
                severity=severity,
            )
            s.add(row)
            s.commit()
            out.append(ConflictHit(
                raw_id=raw_id, spec_slug=sh.ref, summary=summary, severity=severity, log_id=row.id,
            ))
    return out


def resolve(
    log_id: int,
    *,
    resolution: str,
    resolver_domain: str = "",
) -> bool:
    """人裁决冲突。resolution: superseded / coexist / rejected。"""
    if resolution not in ("superseded", "coexist", "rejected"):
        raise ValueError(f"resolution must be superseded|coexist|rejected, got {resolution!r}")
    with session() as s:
        row = s.get(ConflictLog, log_id)
        if row is None:
            return False
        row.resolution = resolution
        row.resolved_by = resolver_domain
        row.resolved_at = datetime.now(timezone.utc)
        s.commit()
    return True
