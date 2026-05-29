"""存量 open ConflictLog 重判 — 用新 detector 的 LLM judge 把噪声清掉。

针对一次性场景(detector 收口前已经攒了一堆假冲突)。原则:
- 不新建 ConflictLog,只更新现有行
- verdict=none → resolution=auto_rejected,挂 auto_reason
- contradicts → 走 _decide_resolution,可能 auto_superseded / auto_rejected /
  auto_coexist / 仍 open(severity=high 留人裁)

外部入口:
- rejudge_open_conflicts() -> dict 统计
- 由 /admin/conflicts/rejudge POST 触发
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from helper.conflict.detector import (
    _decide_resolution,
    _judge_pair,
    _normalize_judge,
    _supersede_target,
)
from helper.storage import session
from helper.storage.models import (
    CaseCandidate,
    ConflictLog,
    FactCandidate,
    L1Item,
    RawInput,
    RelationCandidate,
    SpecCandidate,
)

log = logging.getLogger(__name__)


def _load_old_payload(s, target_type: str, target_slug: str) -> tuple[str, dict | str] | None:
    """按 target_type 反查 candidate,组成给 LLM 看的 old_payload。"""
    if target_type == "spec":
        sc = s.execute(select(SpecCandidate).where(SpecCandidate.slug == target_slug)).scalar_one_or_none()
        if sc is None:
            return None
        return f"spec — {sc.title}", (sc.statement or "")[:1200]
    if target_type == "fact":
        fc = s.execute(select(FactCandidate).where(FactCandidate.slug == target_slug)).scalar_one_or_none()
        if fc is None:
            return None
        return "fact", {
            "subject": fc.subject, "predicate": fc.predicate,
            "object": fc.object, "scope": fc.scope or "",
        }
    if target_type == "case":
        cc = s.execute(select(CaseCandidate).where(CaseCandidate.slug == target_slug)).scalar_one_or_none()
        if cc is None:
            return None
        return "case", {
            "scene": cc.scene, "outcome": cc.outcome or "",
            "referenced_spec": cc.referenced_spec or "",
        }
    if target_type == "relation":
        rc = s.execute(select(RelationCandidate).where(RelationCandidate.slug == target_slug)).scalar_one_or_none()
        if rc is None:
            return None
        return "relation", {
            "entity_a": rc.entity_a, "relation": rc.relation, "entity_b": rc.entity_b,
        }
    return None


def _load_new_payload(
    s, raw_id: int, target_type: str
) -> tuple[str, dict, list[str]] | None:
    """从 raw 的 L1Item 里挑出对应 type 的第一条 payload。

    返 (kind, payload, entity_names)。挑不到返 None(说明 L1 没了 / 只有 concept)。
    """
    items = list(s.execute(
        select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
    ).scalars())
    type_map = {"spec": "decision", "fact": "fact", "case": "case", "relation": "relation"}
    want = type_map.get(target_type)
    if want is None:
        return None
    for it in items:
        if it.type != want:
            continue
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        entity_names = []
        for k in ("subject", "scene", "scope", "entity_a", "entity_b"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                entity_names.append(v.strip())
        return want, payload, entity_names
    return None


def rejudge_open_conflicts(*, limit: int = 200) -> dict:
    """跑全部 resolution=open 的 ConflictLog。返统计。

    注意:每条会触发一次 LLM 调用,几十条以内 OK,几百条要放后台。limit 兜底。
    """
    now = datetime.now(timezone.utc)
    stats = {
        "total": 0, "missing_data": 0, "rejected_none": 0,
        "auto_superseded": 0, "auto_rejected": 0, "auto_coexist": 0,
        "kept_open": 0, "errors": 0,
    }
    with session() as s:
        rows = s.execute(
            select(ConflictLog)
            .where(ConflictLog.resolution == "open")
            .order_by(ConflictLog.id)
            .limit(limit)
        ).scalars().all()
    stats["total"] = len(rows)

    for row in rows:
        try:
            with session() as s:
                # 重新 attach 一份(避免跨 session 的 detached 问题)
                cl = s.get(ConflictLog, row.id)
                if cl is None:
                    continue
                target_type = cl.target_type or "spec"
                target_slug = cl.target_slug
                raw = s.get(RawInput, cl.raw_id)
                if raw is None:
                    stats["missing_data"] += 1
                    continue
                raw_text = raw.content_text or ""
                old = _load_old_payload(s, target_type, target_slug)
                new = _load_new_payload(s, cl.raw_id, target_type)
            if old is None or new is None:
                # 候选已 supersede / L1 已被 GC → 这条 conflict 失去依据,直接 close
                with session() as s:
                    cl = s.get(ConflictLog, row.id)
                    if cl is not None and cl.resolution == "open":
                        cl.resolution = "auto_rejected"
                        cl.resolved_by = "auto-rejudge"
                        cl.resolved_at = now
                        cl.auto_reason = "rejudge: 候选或 L1 数据已不存在"
                stats["missing_data"] += 1
                continue

            old_kind, old_payload = old
            new_kind, new_payload, entity_names = new
            judged = _normalize_judge(_judge_pair(
                raw_text=raw_text,
                new_kind=new_kind, new_payload=new_payload,
                old_kind=old_kind, old_payload=old_payload,
                entity_names=entity_names,
            ))

            if judged["verdict"] != "contradicts":
                with session() as s:
                    cl = s.get(ConflictLog, row.id)
                    if cl is not None and cl.resolution == "open":
                        cl.resolution = "auto_rejected"
                        cl.resolved_by = "auto-rejudge"
                        cl.resolved_at = now
                        cl.auto_reason = f"rejudge: verdict={judged['verdict']}(不再判为冲突)"
                stats["rejected_none"] += 1
                continue

            resolution, reason = _decide_resolution(judged)
            with session() as s:
                cl = s.get(ConflictLog, row.id)
                if cl is None or cl.resolution != "open":
                    continue
                # 更新 severity / summary 取自最新判断
                if judged.get("summary"):
                    cl.summary = judged["summary"]
                cl.severity = judged["severity"]
                if resolution == "open":
                    stats["kept_open"] += 1
                    continue
                cl.resolution = resolution
                cl.resolved_by = "auto-rejudge"
                cl.resolved_at = now
                cl.auto_reason = reason
                if resolution == "auto_superseded":
                    _supersede_target(s, cl.target_type or "spec", cl.target_slug, cl.raw_id)
                stats[resolution] += 1
        except Exception:  # noqa: BLE001
            log.exception("rejudge conflict#%d failed", row.id)
            stats["errors"] += 1

    log.info("rejudge done: %s", stats)
    return stats
