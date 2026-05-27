"""Inbox 周报回执 — 解析 owner 私聊里的「批准/驳回/跳过 #N」+「答 #N ...」并派发。

只对配了 helper_owner_domain 的那一人开放,且只在私聊上下文(chat_id 空)。
群聊里不识别 — 避免误触。

支持:
- 批准 #N / approve #N → SpecCandidate.id=N → promote_spec(slug)(若状态仍 pending)
- 驳回 #N / reject #N → SpecCandidate.id=N → review_status='rejected'
- 跳过 #N / skip #N   → 仅日志,不动数据(让它下周继续出现在 inbox)
- 答 #N <文本>        → InquiryLog.id=N → record_answer + 触发 L1 抽答复
- 裸 #N <文本>        → 只有当 N 是未答 InquiryLog.id 时才命中,否则放行

返回值: 给用户的中文回复文案 + (调用方需要调度的副作用)。
不匹配返 None,让上层走 intent classify。

调用 contract: 返 ReplyResult(text, after_actions[(action_name, payload), ...])。
after_actions 现支持:
  - ("schedule_l1", raw_id) — 上层把这个 raw_id 丢给 schedule_l1 跑 L1
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from helper.config import get_settings
from helper.storage import session
from helper.storage.models import InquiryLog, SpecCandidate

log = logging.getLogger(__name__)


@dataclass
class ReplyResult:
    text: str
    after_actions: list[tuple[str, int]] = field(default_factory=list)


# 「批准 #3」「批准3」「批准#3」「approve #3」
_ACTION_RE = re.compile(r"^\s*(批准|驳回|跳过|approve|reject|skip)\s*#?(\d+)\s*$", re.IGNORECASE)
# 显式答: 「答 #3 内容」「答#3 内容」「答3 内容」
_ANSWER_EXPLICIT_RE = re.compile(r"^\s*答\s*#?(\d+)[\s,，:]+(.+)$", re.DOTALL)
# 裸答: 「#3 内容」(N 是 InquiryLog.id 才命中,否则不识别 — 防误触)
_ANSWER_BARE_RE = re.compile(r"^\s*#(\d+)[\s,，:]+(.+)$", re.DOTALL)


def _is_owner(domain: str) -> bool:
    owner = get_settings().helper_owner_domain
    return bool(owner) and domain == owner


def _handle_action(action_raw: str, cand_id: int, sender_domain: str) -> ReplyResult:
    action = {
        "批准": "approve", "approve": "approve",
        "驳回": "reject",  "reject": "reject",
        "跳过": "skip",    "skip": "skip",
    }[action_raw.lower()]

    if action == "skip":
        log.info("inbox skip #%d by %s", cand_id, sender_domain)
        return ReplyResult(text=f"⏭ 已跳过 #{cand_id},下周继续提醒。")

    with session() as s:
        sc = s.get(SpecCandidate, cand_id)
        if sc is None:
            return ReplyResult(text=f"找不到 spec 候选 #{cand_id}")
        if sc.review_status != "pending":
            return ReplyResult(text=f"#{cand_id} 状态已是 {sc.review_status},无需再处理。")
        slug = sc.slug
        title = sc.title
        if action == "reject":
            sc.review_status = "rejected"
            return ReplyResult(text=f"❌ 已驳回 #{cand_id} [{slug}]: {title}")

    if action == "approve":
        try:
            from helper.specgen import promote_spec
            rel = promote_spec(slug, reviewer=sender_domain)
        except Exception as e:  # noqa: BLE001
            log.exception("promote_spec failed slug=%s", slug)
            return ReplyResult(text=f"⚠️ #{cand_id} 晋升失败: {e}")
        if rel is None:
            return ReplyResult(text=f"⚠️ #{cand_id} 找不到 slug={slug}")
        return ReplyResult(text=f"✅ 已批准 #{cand_id} [{slug}] → {rel}")

    return ReplyResult(text="")


def _handle_answer(inquiry_id: int, answer_raw_id: int, sender_domain: str) -> ReplyResult:
    """绑答复 raw 到 InquiryLog,触发 L1 跑答复内容。"""
    from helper.inquiry import record_answer

    with session() as s:
        iq = s.get(InquiryLog, inquiry_id)
        if iq is None:
            return ReplyResult(text=f"找不到追问 #{inquiry_id}")
        if iq.answer_raw_id is not None:
            return ReplyResult(text=f"追问 #{inquiry_id} 已答过(raw#{iq.answer_raw_id}),不重复绑定。")
        question_preview = (iq.question or "")[:60].replace("\n", " ")

    record_answer(inquiry_id, answer_raw_id)
    log.info("inquiry #%d answered by raw#%d (%s)", inquiry_id, answer_raw_id, sender_domain)
    return ReplyResult(
        text=f"📝 已记录对追问 #{inquiry_id} 的答复(raw#{answer_raw_id})。\n问: {question_preview}",
        after_actions=[("schedule_l1", answer_raw_id)],
    )


def try_handle(
    text: str,
    *,
    sender_domain: str,
    chat_id: str,
    answer_raw_id: int = 0,
) -> ReplyResult | None:
    """匹配则处理并返回 ReplyResult;不匹配返 None。

    - chat_id 非空(群聊)直接放行(不识别)
    - sender 不是 owner 也放行
    - answer_raw_id: 当前消息对应的 raw_id,用于「答 #N」绑定
    """
    if chat_id:
        return None
    if not _is_owner(sender_domain):
        return None
    text = text or ""

    # 1) 批准/驳回/跳过 #N
    m = _ACTION_RE.match(text)
    if m is not None:
        return _handle_action(m.group(1), int(m.group(2)), sender_domain)

    # 2) 显式 「答 #N <内容>」
    m = _ANSWER_EXPLICIT_RE.match(text)
    if m is not None:
        if not answer_raw_id:
            return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")
        return _handle_answer(int(m.group(1)), answer_raw_id, sender_domain)

    # 3) 裸 「#N <内容>」— 只有 N 是未答 InquiryLog.id 才命中,否则放行
    m = _ANSWER_BARE_RE.match(text)
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
