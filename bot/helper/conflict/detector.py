"""统一冲突 detector — 任意 L1 原子类型 vs 已有候选。

设计:
- 输入: raw_id;读 raw + 该 raw 的所有 L1Item
- 5 种 type 各走一条策略,落到同一张 conflict_log(target_type 区分)
- 落库后由 inbox 周报或主动 /inbox 推给 owner,owner 用 [采纳/保留/都留 2-N] 裁决

策略:

| L1.type   | 冲突来源       | 判定方式                                              |
|-----------|----------------|-------------------------------------------------------|
| decision  | 已晋升 spec    | LLM judge (conflict_judge) — 同 v0 流程                |
| fact      | fact_candidates | 同 (subject, predicate) 但 object/scope 不同 → 结构判定 |
| case      | case_candidates | 同 referenced_spec 但 outcome 文本不同 → 结构判定        |
| relation  | relation_candidates | 同 (entity_a, entity_b) 但 relation 不同 → 结构判定 |
| concept   | entity_candidates | slug 已 dedup 合并;不再单独检测,留空                |

幂等: (raw_id, target_type, target_slug, resolution=open) 已存在 → 复用旧行不重写。
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
from helper.storage.models import (
    CaseCandidate,
    ConflictLog,
    FactCandidate,
    L1Item,
    RawInput,
    RelationCandidate,
)

log = logging.getLogger(__name__)

_TOP_K_SPECS = 3
_MAX_DECISIONS = 5  # 单条 raw 真有这么多 decision 已经是异常,cap 防 prompt 爆炸


@dataclass
class ConflictHit:
    raw_id: int
    target_type: str  # spec / fact / case / relation
    target_slug: str
    summary: str
    severity: str  # low / medium / high
    log_id: int | None = None

    # 兼容老调用方(cli.py / detector 自身打印)
    @property
    def spec_slug(self) -> str:
        return self.target_slug


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


def _record_conflict(
    raw_id: int,
    target_type: str,
    target_slug: str,
    summary: str,
    severity: str,
) -> ConflictHit | None:
    """落库,幂等。返回 ConflictHit;无 summary 则跳过。"""
    if not target_slug or not summary:
        return None
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    with session() as s:
        existing = s.execute(
            select(ConflictLog)
            .where(ConflictLog.raw_id == raw_id)
            .where(ConflictLog.target_type == target_type)
            .where(ConflictLog.target_slug == target_slug)
            .where(ConflictLog.resolution == "open")
        ).scalar_one_or_none()
        if existing is not None:
            return ConflictHit(
                raw_id=raw_id,
                target_type=target_type,
                target_slug=target_slug,
                summary=summary,
                severity=severity,
                log_id=existing.id,
            )
        row = ConflictLog(
            raw_id=raw_id,
            target_type=target_type,
            target_slug=target_slug,
            summary=summary,
            severity=severity,
        )
        s.add(row)
        s.commit()
        return ConflictHit(
            raw_id=raw_id,
            target_type=target_type,
            target_slug=target_slug,
            summary=summary,
            severity=severity,
            log_id=row.id,
        )


# ---------- decision (LLM judge against spec) ----------

def _detect_decision(
    raw_id: int,
    raw_text: str,
    decisions: list[tuple[int, dict]],
    *,
    top_k_specs: int,
) -> list[ConflictHit]:
    out: list[ConflictHit] = []
    seen_specs: set[str] = set()
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
            hit = _record_conflict(
                raw_id,
                target_type="spec",
                target_slug=sh.ref,
                summary=str(data.get("summary", "")).strip(),
                severity=str(data.get("severity", "medium")).lower(),
            )
            if hit is not None:
                out.append(hit)
    return out


# ---------- fact: 同 (subject,predicate) 但 object/scope 不同 ----------

def _detect_fact(raw_id: int, items: list[L1Item]) -> list[ConflictHit]:
    out: list[ConflictHit] = []
    with session() as s:
        for it in items:
            if it.type != "fact":
                continue
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            subject = str(payload.get("subject", "")).strip()
            predicate = str(payload.get("predicate", "")).strip()
            obj = str(payload.get("object", "")).strip()
            scope = str(payload.get("scope", "")).strip()
            if not (subject and predicate):
                continue

            # 找已存在的同 subject+predicate 不同 object/scope 的 fact
            existing = s.execute(
                select(FactCandidate)
                .where(FactCandidate.subject == subject[:255])
                .where(FactCandidate.predicate == predicate[:255])
                .where(FactCandidate.superseded_at.is_(None))
            ).scalars().all()
            for fc in existing:
                # 跳过自身(本次刚 upsert 的同陈述)
                if fc.object == obj and (fc.scope or "") == scope:
                    continue
                summary = (
                    f"新陈述 {subject} {predicate} 「{obj}」"
                    f"(scope={scope or '-'}) "
                    f"vs 既有 {subject} {predicate} 「{fc.object}」"
                    f"(scope={fc.scope or '-'})"
                )
                hit = _record_conflict(
                    raw_id,
                    target_type="fact",
                    target_slug=fc.slug,
                    summary=summary,
                    severity="medium",
                )
                if hit is not None:
                    out.append(hit)
    return out


# ---------- case: 同 referenced_spec / 同 scene 但 outcome 不同 ----------

def _detect_case(raw_id: int, items: list[L1Item]) -> list[ConflictHit]:
    out: list[ConflictHit] = []
    with session() as s:
        for it in items:
            if it.type != "case":
                continue
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            scene = str(payload.get("scene", "")).strip()
            outcome = str(payload.get("outcome", "")).strip()
            ref_spec = str(payload.get("referenced_spec", "")).strip()
            if not scene or not outcome:
                continue

            # 同 referenced_spec(若有)优先;否则用 scene 完全相同(短串)
            q = select(CaseCandidate).where(CaseCandidate.superseded_at.is_(None))
            if ref_spec:
                q = q.where(CaseCandidate.referenced_spec == ref_spec[:128])
            else:
                # scene 完全相同,长 scene 命中概率低,但够用作零依赖兜底
                q = q.where(CaseCandidate.scene == scene)
            existing = s.execute(q).scalars().all()
            for cc in existing:
                # 同一 case slug 自身,outcome 已合并,跳过
                if (cc.outcome or "") == outcome:
                    continue
                # 进一步要求 scene 至少有重叠
                if not _scenes_similar(cc.scene, scene):
                    continue
                summary = (
                    f"同场景 case 出现新结果: 既有「{(cc.outcome or '')[:80]}」"
                    f" → 新「{outcome[:80]}」"
                )
                hit = _record_conflict(
                    raw_id,
                    target_type="case",
                    target_slug=cc.slug,
                    summary=summary,
                    severity="medium",
                )
                if hit is not None:
                    out.append(hit)
    return out


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _scenes_similar(a: str, b: str) -> bool:
    """简单 jaccard 阈值,>=0.3 视为同场景。"""
    ta = {t.lower() for t in _TOKEN_RE.findall(a or "") if len(t) > 1}
    tb = {t.lower() for t in _TOKEN_RE.findall(b or "") if len(t) > 1}
    if not ta or not tb:
        return False
    overlap = ta & tb
    return len(overlap) / max(len(ta | tb), 1) >= 0.3


# ---------- relation: 同 (a, b) 但 relation 不同 ----------

def _detect_relation(raw_id: int, items: list[L1Item]) -> list[ConflictHit]:
    out: list[ConflictHit] = []
    with session() as s:
        for it in items:
            if it.type != "relation":
                continue
            try:
                payload = json.loads(it.payload_json or "{}")
            except json.JSONDecodeError:
                continue
            a = str(payload.get("entity_a", "")).strip()
            rel = str(payload.get("relation", "")).strip()
            b = str(payload.get("entity_b", "")).strip()
            if not (a and rel and b):
                continue
            existing = s.execute(
                select(RelationCandidate)
                .where(RelationCandidate.entity_a == a[:128])
                .where(RelationCandidate.entity_b == b[:128])
                .where(RelationCandidate.superseded_at.is_(None))
            ).scalars().all()
            for rc in existing:
                if rc.relation == rel:
                    continue
                summary = (
                    f"实体关系新表述: 既有 {a} —[{rc.relation}]→ {b}"
                    f" 与新 {a} —[{rel}]→ {b} 不一致"
                )
                hit = _record_conflict(
                    raw_id,
                    target_type="relation",
                    target_slug=rc.slug,
                    summary=summary,
                    severity="medium",
                )
                if hit is not None:
                    out.append(hit)
    return out


# ---------- 主入口 ----------

def detect_for_raw(raw_id: int, *, top_k_specs: int = _TOP_K_SPECS) -> list[ConflictHit]:
    """对一条 raw 跑全类型冲突检测 → 落 conflict_log。返触发的列表。

    数据源: L1Item;按 type 走对应策略。
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

    decisions: list[tuple[int, dict]] = []
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

    out: list[ConflictHit] = []
    if decisions:
        out.extend(_detect_decision(raw_id, raw_text, decisions, top_k_specs=top_k_specs))
    out.extend(_detect_fact(raw_id, l1_items))
    out.extend(_detect_case(raw_id, l1_items))
    out.extend(_detect_relation(raw_id, l1_items))

    log.info(
        "conflict: raw#%d → %d hit(s) [%s]",
        raw_id, len(out),
        ", ".join(f"{h.target_type}:{h.target_slug}" for h in out),
    )
    return out


# ---------- 裁决 ----------

def resolve(
    log_id: int,
    *,
    resolution: str,
    resolver_domain: str = "",
) -> bool:
    """人裁决冲突。resolution: superseded / coexist / rejected。

    superseded: 旧候选打 superseded_at(retrieve 自动过滤)
    coexist:    都保留(scope/边界不同的并存)
    rejected:   新输入被否决(已落库的 raw 不撤,但既有不动)
    """
    if resolution not in ("superseded", "coexist", "rejected"):
        raise ValueError(
            f"resolution must be superseded|coexist|rejected, got {resolution!r}"
        )
    now = datetime.now(timezone.utc)
    with session() as s:
        row = s.get(ConflictLog, log_id)
        if row is None:
            return False
        row.resolution = resolution
        row.resolved_by = resolver_domain
        row.resolved_at = now

        # superseded: 把对应候选打 superseded_at;若已晋升过 git,把那张 .md 删掉并 commit
        git_to_remove: str | None = None
        if resolution == "superseded":
            target_type = row.target_type or "spec"
            target_slug = row.target_slug
            model_cls = {
                "spec": "SpecCandidate",
                "fact": "FactCandidate",
                "case": "CaseCandidate",
                "concept": "EntityCandidate",
                "relation": "RelationCandidate",
            }.get(target_type)
            if model_cls is not None:
                from helper.storage import models as _m
                cls = getattr(_m, model_cls)
                cand = s.execute(
                    select(cls).where(cls.slug == target_slug)
                ).scalar_one_or_none()
                if cand is not None and cand.superseded_at is None:
                    cand.superseded_at = now
                    cand.superseded_by = row.raw_id
                    if cand.git_path:
                        git_to_remove = cand.git_path
        s.commit()

    if git_to_remove:
        _remove_from_git(git_to_remove, reason=f"conflict#{log_id}")
    return True


def _remove_from_git(rel_path: str, *, reason: str) -> None:
    """从 spec git repo 删一个 .md 并 commit。失败仅 log。"""
    try:
        from git import Repo

        from helper.config import get_settings
        s = get_settings()
        abs_path = s.helper_spec_git_dir / rel_path
        if not abs_path.exists():
            return
        abs_path.unlink()
        repo = Repo(s.helper_spec_git_dir)
        repo.index.remove([rel_path], working_tree=False)
        if repo.is_dirty():
            repo.index.commit(f"supersede: drop {rel_path} ({reason})")
        # 重建 bundle 让 agent 立刻看不到这条
        try:
            from helper.compiler import build_bundle
            build_bundle()
        except Exception:  # noqa: BLE001
            log.exception("rebuild bundle after supersede failed")
    except Exception:  # noqa: BLE001
        log.exception("remove %s from git failed", rel_path)
