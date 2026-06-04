"""统一冲突 detector — 任意 L1 原子类型 vs 已有候选,LLM 单轮判定 + auto-resolve。

设计:
- 5 种 type(decision/fact/case/relation,concept 复用 dedup 不单独检测)结构筛
  出潜在冲突候选,统一塞给同一个 LLM judge prompt
- judge 输出 verdict + severity + auto_resolution + auto_reason
- verdict=none 直接丢弃,不入 ConflictLog(以前漏的最大噪声源)
- verdict=contradicts:
  - LLM 给了 auto_resolution(基于 memory 权威或 newest-wins) → 直接落 auto_*
    并 supersede 旧的(若 superseded);完全静默,不通知 owner
  - LLM 没拍板 → severity=high 留人裁(resolution=open 进周报),否则
    代码兜底 newest-wins(auto_superseded)
- 落库后 auto_* 不进 inbox 待裁段,仅在周报"本周自动裁决"段统计

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
    ConflictLog,
    L1Item,
    Memory,
    RawInput,
)

log = logging.getLogger(__name__)

_TOP_K_SPECS = 3
_MAX_DECISIONS = 5  # 单条 raw 真有这么多 decision 已经是异常,cap 防 prompt 爆炸


@dataclass
class ConflictHit:
    raw_id: int
    target_type: str  # spec / memory
    target_slug: str
    summary: str
    severity: str  # low / medium / high
    resolution: str = "open"  # open / auto_superseded / auto_rejected / auto_coexist
    auto_reason: str = ""
    log_id: int | None = None

    # 兼容老调用方
    @property
    def spec_slug(self) -> str:
        return self.target_slug


SYSTEM_PROMPT = """你是知识库冲突 judge。给你:
1. 一条新输入(raw 原文 + 它抽出的某个原子 payload)
2. 一条已有的同类候选(同 subject/predicate / 同 entity 对 / 同场景 / 命中相关 spec)
3. 可能附「权威规则」段(用户先前在 IM 设的 procedural memory),里面可能写了
   "X 领域以 Y 说的为准"之类的元规则

判断它们是否真冲突,以及能否当场自动裁决。输出 JSON:
{
  "verdict": "contradicts | refines | none",
  "summary": "一句话说明冲突点(verdict!=contradicts 时填空串)",
  "severity": "low | medium | high",
  "auto_resolution": "superseded | rejected | coexist | "",
  "auto_reason": "若 auto_resolution 非空,一句话理由"
}

判断标准:
- contradicts: 在相同 scope/对象上,新旧不可同时成立(不是表述差异 / 占位符 / 同义改写)
- refines:    同方向具体化 / 边界补充,本质相容,verdict=none 即可
- none:       不在同一对象 / paraphrase 改写 / 完全无关

severity:
- high:   颠覆性,影响后续所有同 scope 决策
- medium: 部分冲突,有妥协空间
- low:    边缘冲突,大概率只是表述差异或细枝末节

auto_resolution:
- superseded: 用新覆盖旧(最常见 — newest-wins,新输入更近更应被信)
- rejected:   保留旧,否决新(权威规则要求"以 X 为准"且新输入不是 X)
- coexist:    并存(scope/边界明显不同,可同时成立)
- "":         不确定,留人裁(只在 severity=high 且无明确权威时这么写)

完全无关或 paraphrase 直接 verdict=none + 其余空,不要硬找冲突。
只输出 JSON,不要 markdown。"""


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
        result = json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _related_directives(entity_names: list[str]) -> str:
    """命中 entity 的 alive memory 全拼,让 LLM 自己判是不是权威规则。

    不在代码里硬编码权威规则的语义 — 用户用自然语言写,LLM 推理是否适用。
    """
    refs = {n for n in entity_names if n}
    if not refs:
        # 没 entity 不代表没全局权威规则
        with session() as s:
            globals_only = s.execute(
                select(Memory)
                .where(Memory.superseded_at.is_(None))
                .where(Memory.scope_type == "global")
            ).scalars().all()
        if not globals_only:
            return ""
        return "## 权威规则\n" + "\n".join(f"- {m.directive}" for m in globals_only)
    with session() as s:
        rows = s.execute(
            select(Memory).where(Memory.superseded_at.is_(None))
        ).scalars().all()
    lines = []
    for m in rows:
        if m.scope_type == "global":
            lines.append(f"- {m.directive}")
        elif m.scope_type == "entity" and m.scope_ref in refs:
            lines.append(f"- 涉及『{m.scope_ref}』时:{m.directive}")
    if not lines:
        return ""
    return "## 权威规则\n" + "\n".join(lines)


def _judge_pair(
    *,
    raw_text: str,
    new_kind: str,           # decision/fact/case/relation
    new_payload: dict | str, # decision 是 dict;其他是描述串
    old_kind: str,
    old_payload: dict | str,
    entity_names: list[str],
) -> dict | None:
    """跑一次 LLM judge,返结构化结果。失败返 None。"""
    user_parts = [
        "## 新输入(raw)",
        f"原文: {raw_text[:600]}",
        "",
        f"## 新 {new_kind}",
        json.dumps(new_payload, ensure_ascii=False, indent=2)
        if isinstance(new_payload, dict) else str(new_payload),
        "",
        f"## 已有 {old_kind}",
        json.dumps(old_payload, ensure_ascii=False, indent=2)
        if isinstance(old_payload, dict) else str(old_payload),
    ]
    directives = _related_directives(entity_names)
    if directives:
        user_parts.extend(["", directives])
    try:
        reply = run("conflict_judge", system=SYSTEM_PROMPT, user="\n".join(user_parts))
    except Exception as e:  # noqa: BLE001
        log.warning("conflict_judge LLM failed: %s", e)
        return None
    return _parse_json(reply)


def _normalize_judge(data: dict | None) -> dict:
    """规范化 judge 输出 + 兜底默认值。"""
    if not data:
        return {"verdict": "none"}
    verdict = str(data.get("verdict", "none")).lower()
    severity = str(data.get("severity", "medium")).lower()
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    auto = str(data.get("auto_resolution", "")).lower().strip()
    if auto not in ("superseded", "rejected", "coexist"):
        auto = ""
    return {
        "verdict": verdict,
        "summary": str(data.get("summary", "")).strip(),
        "severity": severity,
        "auto_resolution": auto,
        "auto_reason": str(data.get("auto_reason", "")).strip(),
    }


def _decide_resolution(judged: dict) -> tuple[str, str]:
    """LLM 没给 auto_resolution 时,代码按 severity 兜底。

    返回 (resolution, auto_reason)。resolution=open 表示要人裁。
    """
    auto = judged.get("auto_resolution") or ""
    reason = judged.get("auto_reason") or ""
    severity = judged.get("severity", "medium")
    if auto:
        return f"auto_{auto}", reason or f"LLM judge 自动裁决({auto})"
    if severity == "high":
        return "open", ""
    # 兜底 newest-wins
    return "auto_superseded", "newest-wins 兜底(severity 非 high 且无权威规则命中)"


# ---------- 入库 + supersede ----------

def _supersede_target(s, target_type: str, target_slug: str, raw_id: int) -> None:
    """auto_superseded / superseded 都通过这里给 spec 打 superseded_at。

    打完同时把对应 fts/vector 索引清掉 — 否则 retrieve 仍能召回到已废弃候选。
    fts/vec 失败仅 log,不阻塞 supersede 主流程(下次 rebuild 能补)。
    """
    if target_type != "spec":
        return
    now = datetime.now(timezone.utc)
    from helper.storage.models import SpecCandidate
    cand = s.execute(
        select(SpecCandidate).where(SpecCandidate.slug == target_slug)
    ).scalar_one_or_none()
    if cand is not None and cand.superseded_at is None:
        cand.superseded_at = now
        cand.superseded_by = raw_id
        try:
            from helper.storage import fts as _fts, vector as _vec
            _fts.delete(s, kind="spec", ref=target_slug)
            _vec.delete(s, kind="spec", ref=target_slug)
        except Exception:  # noqa: BLE001
            log.exception("supersede index cleanup failed ref=%s", target_slug)


def _record_conflict(
    raw_id: int,
    target_type: str,
    target_slug: str,
    summary: str,
    severity: str,
    resolution: str,
    auto_reason: str,
) -> ConflictHit | None:
    """落库,幂等。auto_* 立刻置 resolved_*,并 supersede 候选(若需要)。"""
    if not target_slug or not summary:
        return None
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    now = datetime.now(timezone.utc)
    with session() as s:
        # 幂等:同 (raw, target) 已存在任何状态(open / auto_* / 已人裁) → 不重写
        existing = s.execute(
            select(ConflictLog)
            .where(ConflictLog.raw_id == raw_id)
            .where(ConflictLog.target_type == target_type)
            .where(ConflictLog.target_slug == target_slug)
        ).scalar_one_or_none()
        if existing is not None:
            return ConflictHit(
                raw_id=raw_id, target_type=target_type, target_slug=target_slug,
                summary=summary, severity=severity, resolution="open",
                log_id=existing.id,
            )
        row = ConflictLog(
            raw_id=raw_id, target_type=target_type, target_slug=target_slug,
            summary=summary, severity=severity, resolution=resolution,
            auto_reason=auto_reason,
        )
        if resolution.startswith("auto_"):
            row.resolved_by = "auto-judge"
            row.resolved_at = now
            if resolution == "auto_superseded":
                _supersede_target(s, target_type, target_slug, raw_id)
        s.add(row)
        s.commit()
        return ConflictHit(
            raw_id=raw_id, target_type=target_type, target_slug=target_slug,
            summary=summary, severity=severity, resolution=resolution,
            auto_reason=auto_reason, log_id=row.id,
        )


# ---------- decision (LLM judge against spec) ----------

def _decision_query(payload: dict) -> str:
    scene = str(payload.get("scene", "")).strip()
    choice = str(payload.get("choice", "")).strip()
    return f"{scene} {choice}".strip()


def _scene_entities(payload: dict) -> list[str]:
    """从 decision payload 里抽几个 entity 名作为 memory 召回的 hint。"""
    out = []
    for k in ("subject", "scene", "scope", "entity_a", "entity_b"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _detect_decision(
    raw_id: int, raw_text: str, decisions: list[tuple[int, dict]],
    *, top_k_specs: int,
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
            judged = _normalize_judge(_judge_pair(
                raw_text=raw_text,
                new_kind="decision", new_payload=payload,
                old_kind=f"spec — {sh.title}", old_payload=sh.body[:1200],
                entity_names=_scene_entities(payload),
            ))
            if judged["verdict"] != "contradicts":
                continue
            resolution, reason = _decide_resolution(judged)
            hit = _record_conflict(
                raw_id, target_type="spec", target_slug=sh.ref,
                summary=judged["summary"], severity=judged["severity"],
                resolution=resolution, auto_reason=reason,
            )
            if hit is not None:
                out.append(hit)
    return out


# ---------- 主入口 ----------

def detect_for_raw(raw_id: int, *, top_k_specs: int = _TOP_K_SPECS) -> list[ConflictHit]:
    """对一条 raw 跑全类型冲突检测 + auto-resolve → 落 conflict_log。"""
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return []
        raw_text = raw.content_text or ""
        l1_items = list(s.execute(
            select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
        ).scalars())

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

    auto_n = sum(1 for h in out if h.resolution.startswith("auto_"))
    log.info(
        "conflict: raw#%d → %d hit(s) (%d auto-resolved) [%s]",
        raw_id, len(out), auto_n,
        ", ".join(f"{h.target_type}:{h.target_slug}:{h.resolution}" for h in out),
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

        git_to_remove: str | None = None
        if resolution == "superseded":
            target_type = row.target_type or "spec"
            target_slug = row.target_slug
            if target_type == "memory":
                try:
                    mem_id = int(target_slug)
                except (ValueError, TypeError):
                    mem_id = 0
                if mem_id:
                    mem = s.get(Memory, mem_id)
                    if mem is not None and mem.superseded_at is None:
                        mem.superseded_at = now
                        mem.superseded_by = row.raw_id
            elif target_type == "spec":
                from helper.storage.models import SpecCandidate
                cand = s.execute(
                    select(SpecCandidate).where(SpecCandidate.slug == target_slug)
                ).scalar_one_or_none()
                if cand is not None and cand.superseded_at is None:
                    cand.superseded_at = now
                    cand.superseded_by = row.raw_id
                    if cand.git_path:
                        git_to_remove = cand.git_path
                    try:
                        from helper.storage import fts as _fts, vector as _vec
                        _fts.delete(s, kind="spec", ref=target_slug)
                        _vec.delete(s, kind="spec", ref=target_slug)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "resolve index cleanup failed ref=%s", target_slug,
                        )
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
        try:
            from helper.compiler import build_bundle
            build_bundle()
        except Exception:  # noqa: BLE001
            log.exception("rebuild bundle after supersede failed")
    except Exception:  # noqa: BLE001
        log.exception("remove %s from git failed", rel_path)


