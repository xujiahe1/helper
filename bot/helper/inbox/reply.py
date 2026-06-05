"""Inbox 周报回执 — 解析 owner 私聊里的指令并派发。

只对配了 helper_owner_domain 的那一人开放,且只在私聊上下文(chat_id 空)。
群聊里不识别 — 避免误触。

支持指令(N 是周报里 1-based 序号,从 owner 最近一次收到的周报里反查真实 ID):

  1) 待沉淀规约 (1-N)
     批准 1-N / 驳回 1-N / 跳过 1-N

  2) 待修正冲突 (2-N)
     采纳 2-N: ConflictLog.resolution='superseded'
     保留 2-N: ConflictLog.resolution='rejected'
     都留 2-N: ConflictLog.resolution='coexist'

  3) 待答追问主题组 (3-N)
     答 3-N <文本>:
       - 3-N 现在对应一个**追问主题组**(可能含 1..N 条子追问)
       - LLM 判 owner 答案语义覆盖了哪些子追问 → 全部一起 close
       - 没覆盖的子追问留 open,下次周报继续出现

  4) memory_audit 首次确认
     确认 audit / 跳过 audit
       - 确认: pending_audit 列表全部真 supersede
       - 跳过: 仅清空 pending_audit,本次不动 memory(后续每周自动跑 = 不再 dry-run)

  兼容老格式:
  - 批准/驳回/跳过 #N: 仍按 SpecCandidate.id 直接处理
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
from helper.llm import run
from helper.storage import session
from helper.storage.models import (
    ConflictLog,
    InboxDigest,
    InquiryLog,
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
    r"^\s*答\s*3-(\d+)[\s,，::]+(.+)$", re.DOTALL
)
# 单行宽松匹配「3-N <答案>」: 多行批量回执时按行扫,允许省「答」字。
# 字符集容错: 空白/英文逗号/中文逗号/英文冒号/中文冒号/顿号/连字符/破折号。
_LINE_ANSWER_LOOSE_RE = re.compile(
    r"^\s*(?:答\s*)?3-(\d+)[\s,，::、\-—]+(\S.*?)\s*$"
)
# 「反向追问」短语 — 用户在 3-N 行写的不是答案, 而是想让 bot 反过来给解释。
# batch 路径里命中这条的行**不**进 record_answer, 留 open 让下次周报继续出。
# 仅匹配整行 = 反问意图(末尾允许标点/空白), 避免把"展开说说方案 X 的细节"这种
# 包了反问短语的真答案误吃。 大小写无关。
_BACKQUERY_PATTERNS = (
    "展开说说", "详细说说", "再说说", "说详细点", "说详细些",
    "详细解释", "解释下", "解释一下", "讲讲", "具体讲讲", "讲一下",
    "说说看", "说说", "细说", "展开讲讲", "展开",
)
_BACKQUERY_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(p) for p in _BACKQUERY_PATTERNS) + r")\s*[。.!!??~ ]*\s*$"
)
# memory_audit 首跑 dry-run 确认指令
_AUDIT_CONFIRM_RE = re.compile(r"^\s*确认\s*audit\s*$", re.IGNORECASE)
_AUDIT_SKIP_RE = re.compile(r"^\s*跳过\s*audit\s*$", re.IGNORECASE)
# 兼容老格式
_LEGACY_ACTION_RE = re.compile(r"^\s*(批准|驳回|跳过|approve|reject|skip)\s*#?(\d+)\s*$", re.IGNORECASE)
_LEGACY_ANSWER_EXPLICIT_RE = re.compile(r"^\s*答\s*#?(\d+)[\s,，:]+(.+)$", re.DOTALL)
_LEGACY_ANSWER_BARE_RE = re.compile(r"^\s*#(\d+)[\s,，:]+(.+)$", re.DOTALL)


# ---------- helpers ----------


def _humanize_target(s, target_type: str, slug: str) -> str:
    """把 target_type+slug 转成给用户看的人话标签。失败回落 slug。"""
    type_label = {"spec": "规约", "memory": "偏好"}.get(target_type, target_type)
    try:
        if target_type == "spec":
            row = s.execute(select(SpecCandidate).where(SpecCandidate.slug == slug)).scalar_one_or_none()
            if row is not None:
                return f"{type_label} · {row.title}"
        elif target_type == "memory":
            from helper.storage.models import Memory
            try:
                mem_id = int(slug)
            except (ValueError, TypeError):
                mem_id = 0
            if mem_id:
                row = s.get(Memory, mem_id)
                if row is not None:
                    scope = f"{row.scope_ref}" if row.scope_type == "entity" else "全局"
                    return f"{type_label} · [{scope}] {row.directive[:60]}"
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
    """1-N / 2-N 取单个 id (specs / conflicts);3-N 用 _resolve_inquiry_group 取 id 列表。"""
    key = {1: "specs", 2: "conflicts"}.get(section)
    if key is None:
        return None
    arr = payload.get(key) or []
    if not isinstance(arr, list) or n < 1 or n > len(arr):
        return None
    val = arr[n - 1]
    return int(val) if isinstance(val, (int, str)) and str(val).isdigit() else None


def _resolve_inquiry_group(payload: dict, n: int) -> list[int] | None:
    """3-N → 该追问主题组的子追问 id 列表(≥1 条)。

    新格式:payload['inquiries'] = [[id1, id2, ...], [id3], ...]
    老格式向后兼容:payload['inquiries'] = [id1, id2, ...](每条都当独立组)
    """
    arr = payload.get("inquiries") or []
    if not isinstance(arr, list) or n < 1 or n > len(arr):
        return None
    val = arr[n - 1]
    if isinstance(val, list):
        out: list[int] = []
        for x in val:
            if isinstance(x, int):
                out.append(x)
            elif isinstance(x, str) and x.isdigit():
                out.append(int(x))
        return out or None
    if isinstance(val, int):
        return [val]
    if isinstance(val, str) and val.isdigit():
        return [int(val)]
    return None


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
    """老格式 / 老调用方:单条 inquiry 直接记录答复。"""
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


_ANSWER_MATCH_SYSTEM_PROMPT = """你判断答复语义覆盖了哪些子问题。

输入:owner 的答案文本 + 一组未答子追问(各带 id)。
对每条子追问判断:
- 答案是否回答了它(完整回答 / 部分回答都算 "covered=true")
- 完全没涉及到的子问题 covered=false

输出 JSON:
{
  "matches": [
    {"id": <inquiry_id>, "covered": true|false, "reason": "<一句话>"}
  ]
}

只输出 JSON,不要 markdown。所有输入 id 都要在 matches 里出现一次。"""


def _judge_answer_coverage(
    answer: str, inquiries: list[InquiryLog]
) -> dict[int, bool] | None:
    """LLM 判答案覆盖了哪些子追问。失败返 None,调用方 fallback 全部 close。"""
    if not inquiries:
        return {}
    refs = "\n".join(
        f"[id={iq.id}] {(iq.question or '').strip()}"
        for iq in inquiries
    )
    user_msg = (
        f"## owner 答案\n{answer.strip()}\n\n"
        f"## 子追问列表\n{refs}\n\n## 输出\nJSON。"
    )
    try:
        reply = run(
            "inquiry_answer_match",
            system=_ANSWER_MATCH_SYSTEM_PROMPT,
            user=user_msg,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("inquiry_answer_match LLM failed: %s", e)
        return None
    text = (reply or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return None
    out: dict[int, bool] = {}
    for m in matches:
        if not isinstance(m, dict):
            continue
        try:
            iq_id = int(m.get("id"))
        except (TypeError, ValueError):
            continue
        out[iq_id] = bool(m.get("covered"))
    return out


def _handle_batch_answers(
    items: list[tuple[int, str]],
    answer_raw_id: int,
    sender_domain: str,
    *,
    backqueries: list[int] | None = None,
) -> ReplyResult:
    """处理一条消息里多行 3-N 批量回执。

    items: [(n, answer_text), ...] — 真答复行, 走 _handle_answer_group(LLM 判覆盖,
        覆盖到的子追问 record_answer + close)。 一条消息只算一次 schedule_l1。
    backqueries: [n, ...] — 反向追问行(答复文本 = "展开说说"/"讲讲"等), 不动这些
        主题组的子追问, 留 open 下次周报继续。
    """
    backqueries = backqueries or []
    payload = _load_digest_payload(sender_domain)
    if payload is None:
        return ReplyResult(text="⚠️ 还没有最近的 inbox 周报记录,请先发一次「/inbox」。")

    if not answer_raw_id and items:
        # 真答复需要 raw_id 绑定; 纯反问无答可记, 不强求 raw_id
        return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")

    head = f"📝 已批量记录 {len(items)} 条答复(raw#{answer_raw_id})。" if items else "📝 收到批量回复。"
    parts: list[str] = [head]
    handled_groups = 0
    closed_total = 0
    skipped_total = 0
    for n, answer_text in items:
        ids = _resolve_inquiry_group(payload, n)
        if not ids:
            parts.append(f"\n⚠️ 3-{n} 在最近一次周报里找不到,跳过。")
            continue
        # 借用 _handle_answer_group 内部逻辑, 但不重复 raw 绑定 / schedule_l1
        # 直接构造一个独立的 sub-result, 把它的关闭/跳过统计合并进 parts
        sub = _handle_answer_group(ids, answer_text, answer_raw_id, sender_domain)
        handled_groups += 1
        # 抠 sub.text 头部的 "已记录答复" 那行去掉, 保留下面的关闭/跳过明细
        sub_lines = sub.text.split("\n")
        body_lines = [ln for ln in sub_lines if not ln.startswith("📝 已记录答复")]
        parts.append(f"\n— 3-{n} —")
        parts.extend(body_lines)
        # 顺手统计 (粗略 — sub.text 里有"关闭 N 条"形态)
        for ln in body_lines:
            if "关闭" in ln and "条子追问" in ln:
                m = re.search(r"关闭\s+(\d+)\s+条", ln)
                if m:
                    closed_total += int(m.group(1))
            if "留 open" in ln:
                m = re.search(r"留\s*open[^\d]*(\d+)\s*条", ln)
                if m:
                    skipped_total += int(m.group(1))

    if backqueries:
        parts.append(
            f"\n\n🔁 识别到 {len(backqueries)} 条反向追问(展开说说/讲讲等), "
            f"未当作答复, 这些主题留 open 下次周报继续: "
            + ", ".join(f"3-{n}" for n in backqueries)
        )

    parts.append(
        f"\n\n汇总: 处理 {handled_groups} 个主题组, "
        f"共关闭 {closed_total} 条子追问, {skipped_total} 条留 open"
        + (f", {len(backqueries)} 条反问留 open" if backqueries else "")
        + "。"
    )
    actions: list[tuple[str, int]] = []
    if items and answer_raw_id:
        actions.append(("schedule_l1", answer_raw_id))
    return ReplyResult(text="".join(parts), after_actions=actions)


def _handle_answer_group(
    inquiry_ids: list[int], answer_text: str, answer_raw_id: int, sender_domain: str
) -> ReplyResult:
    """3-N 主题组答复 — owner 一次回,LLM 判覆盖了哪些子追问,全部一起 close。

    inquiry_ids 是该 3-N 主题下所有子追问 id(可能 1 条 = 独立追问)。
    """
    from helper.inquiry import record_answer

    if not inquiry_ids:
        return ReplyResult(text="⚠️ 找不到对应子追问。")

    # 拉所有子追问 (跳过已答的)
    with session() as s:
        rows = list(s.execute(
            select(InquiryLog).where(InquiryLog.id.in_(inquiry_ids))
        ).scalars())
        for r in rows:
            s.expunge(r)
    open_rows = [iq for iq in rows if iq.answer_raw_id is None]
    already_answered = [iq for iq in rows if iq.answer_raw_id is not None]

    if not open_rows:
        return ReplyResult(text="该主题下所有子追问之前已答过,不重复绑定。")

    # 单条直接全 close (不调 LLM 省成本); 多条走 LLM 判覆盖
    if len(open_rows) == 1:
        coverage = {open_rows[0].id: True}
    else:
        coverage = _judge_answer_coverage(answer_text, open_rows)
        if coverage is None:
            # LLM 失败 fallback: 全部当 covered (owner 给的答案是给主题组用的, 全 close 是合理默认)
            log.warning("answer coverage LLM failed; fallback close all in group")
            coverage = {iq.id: True for iq in open_rows}

    closed: list[InquiryLog] = []
    skipped: list[InquiryLog] = []
    for iq in open_rows:
        if coverage.get(iq.id, False):
            record_answer(iq.id, answer_raw_id)
            closed.append(iq)
        else:
            skipped.append(iq)

    log.info(
        "inquiry group answer: closed=%d skipped=%d (raw#%d, %s)",
        len(closed), len(skipped), answer_raw_id, sender_domain,
    )

    parts = [f"📝 已记录答复(raw#{answer_raw_id})。"]
    if closed:
        parts.append(f"\n✓ 关闭 {len(closed)} 条子追问:")
        for iq in closed[:5]:
            parts.append(f"  · {(iq.question or '')[:60].replace(chr(10), ' ')}")
        if len(closed) > 5:
            parts.append(f"  ... 另 {len(closed) - 5} 条")
    if skipped:
        parts.append(f"\n↻ 留 open(下次周报继续) {len(skipped)} 条:")
        for iq in skipped[:3]:
            parts.append(f"  · {(iq.question or '')[:60].replace(chr(10), ' ')}")
    if already_answered:
        parts.append(f"\n(另有 {len(already_answered)} 条本组之前已答, 跳过)")

    return ReplyResult(
        text="\n".join(parts),
        after_actions=[("schedule_l1", answer_raw_id)],
    )


# ---------- memory_audit 首跑确认 ----------


def _handle_audit_action(action: str, sender_domain: str) -> ReplyResult:
    """action ∈ {'confirm', 'skip'}。"""
    payload = _load_digest_payload(sender_domain)
    if payload is None:
        return ReplyResult(text="⚠️ 还没有最近的 inbox 周报记录,请先发一次「/inbox」。")
    pending = payload.get("pending_audit") or []
    if not isinstance(pending, list) or not pending:
        return ReplyResult(text="本次周报没有 audit 待确认项。")

    if action == "skip":
        # 清掉 pending_audit 但不动 memory; 下次周报重跑 audit 仍是 dry-run
        # (因为 _is_first_run 看的是 Memory.superseded_by=0 是否存在, 而不是 pending_audit)
        payload.pop("pending_audit", None)
        with session() as s:
            row = s.get(InboxDigest, sender_domain)
            if row is not None:
                row.items_json = json.dumps(payload, ensure_ascii=False)
        return ReplyResult(
            text=f"⏭ 已跳过本次 audit({len(pending)} 条 directive 保留原状)。"
                 f"\n下次周报会重新预审 — 若仍想保持自动模式,回「确认 audit」一次性处理。"
        )

    # confirm
    from helper.memory import apply_pending
    n = apply_pending(pending)
    payload.pop("pending_audit", None)
    with session() as s:
        row = s.get(InboxDigest, sender_domain)
        if row is not None:
            row.items_json = json.dumps(payload, ensure_ascii=False)
    return ReplyResult(
        text=f"✅ 已 supersede {n} 条疑似误抽 directive。\n"
             f"后续每周自动跑 audit,不再询问 — 若误判可在 git/sqlite 里手动恢复。"
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

    # 0) memory_audit 首跑确认
    if _AUDIT_CONFIRM_RE.match(text):
        return _handle_audit_action("confirm", sender_domain)
    if _AUDIT_SKIP_RE.match(text):
        return _handle_audit_action("skip", sender_domain)

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

    # 2) 周报式: 答 3-N <文本> — 走主题组路径,LLM 判覆盖 → 一答 close 多条
    m = _SECTION_ANSWER_RE.match(text)
    if m is not None:
        n = int(m.group(1))
        answer_text = (m.group(2) or "").strip()
        if not answer_raw_id:
            return ReplyResult(text="⚠️ 内部异常:答复消息没有 raw_id,请稍后重试。")
        payload = _load_digest_payload(sender_domain)
        if payload is None:
            return ReplyResult(text="⚠️ 还没有最近的 inbox 周报记录,请先发一次「/inbox」。")
        ids = _resolve_inquiry_group(payload, n)
        if not ids:
            return ReplyResult(text=f"⚠️ 3-{n} 在最近一次周报里找不到,请核对编号。")
        return _handle_answer_group(ids, answer_text, answer_raw_id, sender_domain)

    # 2.5) 多行批量回执: 一条消息含 ≥ 2 行形如「3-N <答案>」(可省「答」字)
    # 例: owner 拷贝周报里 14 个 3-N 主题组, 每行写一个答案。
    # 单行省「答」也能命中(只 1 个匹配也走批量路径, 不影响单条)。
    # 反向追问行(answer 文本 = "展开说说"/"讲讲"等)单独归到 backqueries,
    # 不进 record_answer, 留 open 下次周报继续出。
    lines = [ln for ln in text.splitlines() if ln.strip()]
    batch: list[tuple[int, str]] = []
    backqueries: list[int] = []
    for ln in lines:
        m = _LINE_ANSWER_LOOSE_RE.match(ln)
        if m is None:
            continue
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        ans = (m.group(2) or "").strip()
        if not ans:
            continue
        if _BACKQUERY_RE.match(ans):
            backqueries.append(n)
        else:
            batch.append((n, ans))
    matched = len(batch) + len(backqueries)
    # 至少要 2 行命中(含反问行), 才认这是批量回执 — 避免普通闲聊误触发
    if matched >= 2 and matched >= max(1, len(lines) // 2):
        return _handle_batch_answers(batch, answer_raw_id, sender_domain, backqueries=backqueries)

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
