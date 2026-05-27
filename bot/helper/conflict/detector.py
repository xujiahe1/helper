"""LLM judge 冲突 — 走 conflict_judge(claude-opus-4-7)。

设计:
- 输入: raw_id;读 raw + 该 raw 的 type=decision L1Item(payload_json 内含 scene/choice/rationale)
- 每条 decision 用 scene+choice 检索 bundle 已晋升的 spec,top_k=3
- 对每个 (decision, spec) pair 喂 conflict_judge LLM,verdict=contradicts → 落 ConflictLog
- 幂等: (raw_id, spec_slug, resolution=open) 已存在 → 复用旧行不重写

注意:
- v0 不在 sink._run_consumers 自动跑 — 一次 raw 接收要进 4 个 LLM 已经多了,detector 留给
  manual / batch / specgen promotion 前 gate。先把 API 跑通,后续再决定挂哪。
- ConflictLog 无 decision idx 字段:同 raw 的多个 decision 都撞同一 spec 时,只记一行。
"""

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
from helper.storage.models import ConflictLog, L1Item, RawInput

log = logging.getLogger(__name__)

_TOP_K_SPECS = 3
_MAX_DECISIONS = 5  # 单条 raw 真有这么多 decision 已经是异常,cap 防 prompt 爆炸


@dataclass
class ConflictHit:
    raw_id: int
    spec_slug: str
    summary: str
    severity: str  # low / medium / high
    log_id: int | None = None


SYSTEM_PROMPT = """你是决策规约冲突 judge。给你:
1. 一条新输入(raw 原文 + 一条 L1 抽出的 decision payload)
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
    text = (text or "").strip()
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


def _format_pair(raw_text: str, decision_payload: dict, spec_body: str, spec_title: str) -> str:
    parts = [
        "## 新输入(raw)",
        f"原文: {raw_text[:600]}",
        "",
        "## 这条 raw 抽出的 decision",
        json.dumps(decision_payload, ensure_ascii=False, indent=2),
        "",
        f"## 已有 spec — {spec_title}",
        spec_body[:1200],
    ]
    return "\n".join(parts)


def _decision_query(payload: dict) -> str:
    """决策检索查询 — scene + choice 拼起来命中相关 spec。"""
    scene = str(payload.get("scene", "")).strip()
    choice = str(payload.get("choice", "")).strip()
    return f"{scene} {choice}".strip()


def detect_for_raw(raw_id: int, *, top_k_specs: int = _TOP_K_SPECS) -> list[ConflictHit]:
    """对一条 raw 检索相关 spec → judge → 落冲突。返触发的列表。

    数据源: L1Item(type=decision)的 payload_json,不再读老 L1Result.scene/choice。
    """
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return []
        raw_text = raw.content_text or ""
        l1_items = list(
            s.execute(
                select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
            ).scalars()
        )

    decisions: list[tuple[int, dict]] = []  # (l1_idx, payload)
    for it in l1_items:
        if it.type != "decision":
            continue
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        decisions.append((it.idx, payload))
        if len(decisions) >= _MAX_DECISIONS:
            break

    if not decisions:
        return []

    out: list[ConflictHit] = []
    seen_specs: set[str] = set()  # 同 raw 撞同 spec 不重复 judge

    for _idx, payload in decisions:
        query = _decision_query(payload)
        if not query:
            continue
        hits = retrieve_relevant(query, top_k=top_k_specs)
        spec_hits = [h for h in hits if h.type == "spec"]
        for sh in spec_hits:
            if sh.ref in seen_specs:
                continue
            seen_specs.add(sh.ref)

            prompt = _format_pair(raw_text, payload, sh.body, sh.title)
            try:
                reply = run("conflict_judge", system=SYSTEM_PROMPT, user=prompt)
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
                    out.append(ConflictHit(
                        raw_id=raw_id, spec_slug=sh.ref, summary=summary,
                        severity=severity, log_id=existing.id,
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
                    raw_id=raw_id, spec_slug=sh.ref, summary=summary,
                    severity=severity, log_id=row.id,
                ))

    log.info("conflict: raw#%d → %d contradiction(s) [%s]",
             raw_id, len(out), ", ".join(h.spec_slug for h in out))
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
