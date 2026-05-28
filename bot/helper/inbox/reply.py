"""Inbox 周报回执 — 解析 owner 私聊里的指令并派发。

只对配了 helper_owner_domain 的那一人开放,且只在私聊上下文(chat_id 空)。
群聊里不识别 — 避免误触。

支持指令(N 是周报里 1-based 序号,从 owner 最近一次收到的周报里反查真实 ID):

  1) 待沉淀规约 (1-N)
     批准 1-N / 驳回 1-N / 跳过 1-N
       - 批准: SpecCandidate.id=N → promote_spec(slug)
       - 驳回: review_status='rejected'
       - 跳过: 仅日志,不改状态(下周继续出现)

  2) 待修正冲突 (2-N)
     采纳 2-N: ConflictLog.resolution='superseded' (新覆盖旧,旧候选打 superseded_at)
     保留 2-N: ConflictLog.resolution='rejected'   (新被否决,既有不动)
     都留 2-N: ConflictLog.resolution='coexist'    (并存,不动)

  3) 待答追问 (3-N)
     答 3-N <文本>: InquiryLog.id=N → record_answer + 触发 L1 抽答复

  兼容老格式:
  - 批准/驳回/跳过 #N: 仍按 SpecCandidate.id 直接处理(给跨周老候选用)
  - 答 #N <文本> / #N <文本>(裸): 按 InquiryLog.id 直接处理

返回值: 给用户的中文回复文案 + 副作用列表(after_actions)。
不匹配返 None,让上层走 intent classify。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from helper.config import get_settings
from helper.storage import session
from helper.storage.models import (
    CaseCandidate,
    ConflictLog,
    EntityCandidate,
    FactCandidate,
    InboxDigest,
    InquiryLog,
    RelationCandidate,
    SpecCandidate,
)

log = logging.getLogger(__name__)


@dataclass
class ReplyResult:
    text: str
    after_actions: list[tuple[str, int]] = field(default_factory=list)


# 周报编号: 「批准 1-3」「采纳 2-1」「都留 2-2」「答 3-2 内容」
_SECTION_ACTION_RE = re.compile(
    r"^\s*(批准|驳回|跳过|采纳|保留|都留|approve|reject|skip)\s*([123])-(\d+)\s*$",
    re.IGNORECASE,
)
_SECTION_ANSWER_RE = re.compile(
    r"^\s*答\s*3-(\d+)[\s,，:]+(.+)$", re.DOTALL
)
# 兼容老格式
_LEGACY_ACTION_RE = re.compile(r"^\s*(批准|驳回|跳过|approve|reject|skip)\s*#?(\d+)\s*$", re.IGNORECASE)
_LEGACY_ANSWER_EXPLICIT_RE = re.compile(r"^\s*答\s*#?(\d+)[\s,，:]+(.+)$", re.DOTALL)
_LEGACY_ANSWER_BARE_RE = re.compile(r"^\s*#(\d+)[\s,，:]+(.+)$", re.DOTALL)


# ---------- helpers ----------


def _humanize_target(s, target_type: str, slug: str) -> str:
    """把 target_type+slug 转成给用户看的人话标签。失败回落 slug。"""
    type_label = {
        "spec": "规约", "fact": "事实", "case": "案例",
        "concept": "概念", "relation": "关系",
    }.get(target_type, target_type)
    try:
        if target_type == "fact":
            row = s.execute(select(FactCandidate).where(FactCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.subject} {row.predicate} {row.object}".strip()
        elif target_type == "spec":
            row = s.execute(select(SpecCandidate).where(SpecCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.title}"
        elif target_type == "case":
            row = s.execute(select(CaseCandidate).where(CaseCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.title}"
        elif target_type == "relation":
            row = s.execute(select(RelationCandidate).where(RelationCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.entity_a} —[{row.relation}]→ {row.entity_b}"
        elif target_type == "concept":
            row = s.execute(select(EntityCandidate).where(EntityCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.name}"
    except Exception:  # noqa: BLE001
        pass
    return f"{type_label}"


def _is_owner(domain: str) -> bool:
    owner = get_settings().helper_owner_domain
    return bool(owner) and domain == owner


def _load_digest_payload(owner: str) -> dict | None:
    with session() as s:
        row = s.get(InboxDigest, owner)
        if row is None:
            return None
        try:
            payload = json.loads(row.items_json or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload


def _resolve_section_id(payload: dict, section: int, n: int) -> int | None:
    key = {1: "specs", 2: "conflicts", 3: "inquiries"}.get(section)
    if key is None:
        return None
    arr = payload.get(key) or []
    if not isinstance(arr, list) or n < 1 or n > len(arr):
        return None
    val = arr[n - 1]
    return int(val) if isinstance(val, (int, str)) and str(val).isdigit() else None


# ---------- spec 候选 ----------


def _handle_spec_action(action: str, cand_id: int, sender_domain: str) -> ReplyResult:
    if action == "skip":
        log.info("inbox skip spec#%d by %s", cand_id, sender_domain)
        return ReplyResult(text=f"⏭ 已跳过 1-条目(spec#{cand_id}),下周继续提醒。")

    with session() as s:
        sc = s.get(SpecCandidate, cand_id)
        if sc is None:
            return ReplyResult(text=f"找不到 spec 候选 spec#{cand_id}")
        if sc.review_status != "pending":
            return ReplyResult(text=f"该规约状态已是 {sc.review_status},无需再处理。")
        slug = sc.slug
        title = sc.title
        if action == "reject":
            sc.review_status = "rejected"
            return ReplyResult(text=f"❌ 已驳回 [{slug}]: {title}")

    # approve
    try:
        from helper.specgen import promote_spec
        rel = promote_spec(slug, reviewer=sender_domain)
    except Exception as e:  # noqa: BLE001
        log.exception("promote_spec failed slug=%s", slug)
        return ReplyResult(text=f"⚠️ 晋升失败: {e}")
    if rel is None:
        return ReplyResult(text=f"⚠️ 找不到 slug={slug}")
    return ReplyResult(text=f"✅ 已批准 [{slug}] → {rel}")


# ---------- 冲突 ----------


def _handle_conflict_action(action: str, log_id: int, sender_domain: str) -> ReplyResult:
    """2-N 指令:
    采纳/approve → resolution=superseded(用新覆盖旧,旧候选打 superseded_at)
    保留/reject → resolution=rejected
    都留/coexist → resolution=coexist
    """
    resolution_map = {
        "采纳": "superseded", "approve": "superseded",
        "保留": "rejected",   "reject": "rejected",
        "都留": "coexist",    "coexist": "coexist",
        "skip": None, "跳过": None,
    }
    resolution = resolution_map.get(action.lower())
    if resolution is None:
        log.info("inbox skip conflict#%d by %s", log_id, sender_domain)
        return ReplyResult(text=f"⏭ 已跳过冲突 conflict#{log_id},下周继续提醒。")

    with session() as s:
        row = s.get(ConflictLog, log_id)
        if row is None:
            return ReplyResult(text=f"找不到冲突 conflict#{log_id}")
        if row.resolution != "open":
            return ReplyResult(text=f"该冲突已裁决({row.resolution}),无需再处理。")
        target_label = _humanize_target(s, row.target_type or "spec", row.target_slug)

    try:
        from helper.conflict import resolve as do_resolve
        ok = do_resolve(log_id, resolution=resolution, resolver_domain=sender_domain)
    except Exception as e:  # noqa: BLE001
        log.exception("resolve conflict#%d failed", log_id)
        return ReplyResult(text=f"⚠️ 裁决失败: {e}")
    if not ok:
        return ReplyResult(text=f"⚠️ 裁决失败: 找不到 conflict#{log_id}")

    label = {
        "superseded": "✅ 已采纳新输入(覆盖旧)",
        "rejected":   "❌ 已保留旧版(驳回新)",
        "coexist":    "🔀 已标记并存",
    }[resolution]
    return ReplyResult(text=f"{label} → {target_label}")


# ---------- 追问回答 ----------


def _handle_answer(inquiry_id: int, answer_raw_id: int, sender_domain: str) -> ReplyResult:
    from helper.inquiry import record_answer

    with session() as s:
        iq = s.get(InquiryLog, inquiry_id)
        if iq is None:
            return ReplyResult(text=f"找不到追问 inquiry#{inquiry_id}")
        if iq.answer_raw_id is not None:
            return ReplyResult(text=f"该追问已答过(raw#{iq.answer_raw_id}),不重复绑定。")
        question_preview = (iq.question or "")[:60].replace("\n", " ")

    record_answer(inquiry_id, answer_raw_id)
    log.info("inquiry #%d answered by raw#%d (%s)", inquiry_id, answer_raw_id, sender_domain)
    return ReplyResult(
        text=f"📝 已记录答复(raw#{answer_raw_id})。\n问: {question_preview}",
        after_actions=[("schedule_l1", answer_raw_id)],
    )


# ---------- 主入口 ----------


def try_handle(
    text: str,
    *,
    sender_domain: str,
    chat_id: str,
    answer_raw_id: int = 0,
) -> ReplyResult | None:
    """匹配则处理并返回 ReplyResult;不匹配返 None。

    - chat_id 非空(群聊)直接放行
    - sender 不是 owner 也放行
    - answer_raw_id: 当前消息对应的 raw_id,用于「答 3-N」绑定
    """
    if chat_id:
        return None
    if not _is_owner(sender_domain):
        return None
    text = text or ""

    # 1) 周报式: 批准/驳回/跳过/采纳/保留/都留 1-N | 2-N | 3-N
    m = _SECTION_ACTION_RE.match(text)
    if m is not None:
        action = m.group(1).lower()
        section = int(m.group(2))
        n = int(m.group(3))
        payload = _load_digest_payload(sender_domain)
        if payload is None:
            return ReplyResult(text="⚠️ 还没有最近的 inbox 周报记录,请先发一次「/inbox」。")
        target_id = _resolve_section_id(payload, section, n)
        if target_id is None:
            return ReplyResult(text=f"⚠️ {section}-{n} 在最近一次周报里找不到,请核对编号。")
        if section == 1:
            spec_action = {"批准": "approve", "approve": "approve",
                           "驳回": "reject", "reject": "reject",
                           "跳过": "skip", "skip": "skip"}.get(action)
            if spec_action is None:
                return ReplyResult(text=f"⚠️ 1-N 仅支持「批准 / 驳回 / 跳过」,收到「{action}」。")
            return _handle_spec_action(spec_action, target_id, sender_domain)
        if section == 2:
            return _handle_conflict_action(action, target_id, sender_domain)
        if section == 3:
            # 「答」走 _SECTION_ANSWER_RE,这里不会到;到了说明误用了批准/驳回 + 3-N
            return ReplyResult(text="⚠️ 3-N(追问)请用「答 3-N 你的答案」。")

    # 2) 周报式: 答 3-N <文本>
    m = _SECTION_ANSWER_RE.match(text)
    if m is not None:
        n = int(m.group(1))
        if not answer_raw_id:
            return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")
        payload = _load_digest_payload(sender_domain)
        if payload is None:
            return ReplyResult(text="⚠️ 还没有最近的 inbox 周报记录,请先发一次「/inbox」。")
        target_id = _resolve_section_id(payload, 3, n)
        if target_id is None:
            return ReplyResult(text=f"⚠️ 3-{n} 在最近一次周报里找不到,请核对编号。")
        return _handle_answer(target_id, answer_raw_id, sender_domain)

    # 3) 老格式: 批准/驳回/跳过 #N(直接当 SpecCandidate.id)
    m = _LEGACY_ACTION_RE.match(text)
    if m is not None:
        legacy_action = {"批准": "approve", "approve": "approve",
                         "驳回": "reject", "reject": "reject",
                         "跳过": "skip", "skip": "skip"}[m.group(1).lower()]
        return _handle_spec_action(legacy_action, int(m.group(2)), sender_domain)

    # 4) 老格式: 答 #N <文本> / #N <文本>
    m = _LEGACY_ANSWER_EXPLICIT_RE.match(text)
    if m is not None:
        if not answer_raw_id:
            return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")
        return _handle_answer(int(m.group(1)), answer_raw_id, sender_domain)

    m = _LEGACY_ANSWER_BARE_RE.match(text)
    if m is not None:
        candidate_id = int(m.group(1))
        with session() as s:
            iq = s.get(InquiryLog, candidate_id)
            is_open_inquiry = iq is not None and iq.answer_raw_id is None
        if is_open_inquiry:
            if not answer_raw_id:
                return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")
            return _handle_answer(candidate_id, answer_raw_id, sender_domain)

    return None
