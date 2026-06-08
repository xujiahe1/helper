"""周报构建 + 投递。

数据源 (本周内):
- 新落库 raw 总数 + 出 L1 成功率
- pending review 的 spec_candidates
- open 冲突
- 未答的追问 — 经聚合层(inquiry_group)合并成主题组
- entity 候选数 + 已晋升数

build_digest() 起头会先跑一次 memory_audit:
- 第一次跑(从未做过 audit)→ dry-run, 误抽列表挂 inbox_digest.pending_audit,
  owner 回「确认 audit」决定是否真 supersede
- 后续跑 → 直接 supersede 误抽 directive,周报只渲染 "本周自动 supersede N 条" 摘要

格式: text(M2 起来再上 card)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from helper.im import wave_client
from helper.im.wave_client import WaveAPIError
from helper.inbox.inquiry_group import InquiryGroup, aggregate as aggregate_inquiries
from helper.inquiry import InquiryAuditReport, run_inquiry_audit
from helper.memory import AuditReport, run_audit
from helper.storage import session
from helper.storage.models import (
    ConflictLog,
    InboxDigest,
    InquiryLog,
    L1Result,
    RawInput,
    SpecCandidate,
)

log = logging.getLogger(__name__)


@dataclass
class WeeklyDigest:
    week_start: datetime
    week_end: datetime
    raw_count: int = 0
    l1_ok: int = 0
    l1_err: int = 0
    # 三段编号(1-N / 2-N / 3-N),序号即周报里给用户呈现的编号(从 1 开始)。
    # SpecCandidate.id 一并带上 — 让 owner 用「批准 1-N」回执裁决
    pending_specs: list[tuple[int, str, str]] = field(default_factory=list)  # (id, slug, title)
    # ConflictLog: (id, target_type, target_slug, severity, summary)
    open_conflicts: list[tuple[int, str, str, str, str]] = field(default_factory=list)
    # 追问改成"主题组"形态: 同主题的多条子追问合并成 1 条总问题(3-N),
    # owner 答总问题就能一次 close 多条子追问。
    inquiry_groups: list[InquiryGroup] = field(default_factory=list)
    # memory_audit 摘要: 本次预审 / 自动 supersede 的统计
    audit: AuditReport | None = None
    # inquiry_audit 摘要: 本次自动 close 的过期/学究式追问数
    inquiry_audit: InquiryAuditReport | None = None


def _week_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, now


def build_digest() -> WeeklyDigest:
    # 前置: 先跑一次 memory audit。dry-run(首跑)只挂 pending_audit 不真改库;
    # 自动模式直接 supersede 误抽 directive,这样后面 conflict_log 查询就不会
    # 把已被 audit 干掉的 memory#X 当 open 冲突算进周报。
    audit_report: AuditReport | None
    try:
        audit_report = run_audit()
    except Exception:  # noqa: BLE001
        log.exception("memory audit failed; weekly continues without audit")
        audit_report = None

    # 前置 #2: inquiry audit — 复审存量未答 inquiry 是否仍命中新 G1/G2 判据,
    # 砍掉旧 prompt 留下的学究式追问。 砍后 hit='no' + answer_raw_id=0,
    # 后续 SELECT WHERE answer_raw_id IS NULL 自然过滤掉, 不再进周报。
    inquiry_audit_report: InquiryAuditReport | None
    try:
        inquiry_audit_report = run_inquiry_audit()
    except Exception:  # noqa: BLE001
        log.exception("inquiry audit failed; weekly continues without it")
        inquiry_audit_report = None

    start, end = _week_window()
    d = WeeklyDigest(
        week_start=start, week_end=end,
        audit=audit_report, inquiry_audit=inquiry_audit_report,
    )
    with session() as s:
        d.raw_count = len(s.execute(
            select(RawInput.id).where(RawInput.created_at >= start)
        ).scalars().all())
        d.l1_ok = len(s.execute(
            select(L1Result.raw_id)
            .where(L1Result.created_at >= start)
            .where(L1Result.error == "")
        ).scalars().all())
        d.l1_err = len(s.execute(
            select(L1Result.raw_id)
            .where(L1Result.created_at >= start)
            .where(L1Result.error != "")
        ).scalars().all())

        specs = s.execute(
            select(SpecCandidate)
            .where(SpecCandidate.review_status == "pending")
            .order_by(SpecCandidate.created_at.desc())
            .limit(10)
        ).scalars().all()
        d.pending_specs = [(sc.id, sc.slug, sc.title) for sc in specs]

        conflicts = s.execute(
            select(ConflictLog)
            .where(ConflictLog.resolution == "open")
            .order_by(ConflictLog.created_at.desc())
            .limit(10)
        ).scalars().all()
        d.open_conflicts = [
            (c.id, c.target_type or "spec", c.target_slug, c.severity, (c.summary or "")[:120])
            for c in conflicts
        ]

        # 历史 bug: 旧版本在这里加了 created_at >= 本周开始, 上周没答的追问
        # 这周直接消失。改成全量未答, 用聚合层把多条同主题合并 — 数量爆炸由
        # 聚合 + cap 双管控制, 不再用时间窗截。
        inquiry_rows = list(s.execute(
            select(InquiryLog)
            .where(InquiryLog.answer_raw_id.is_(None))
            .order_by(InquiryLog.created_at.desc())
            .limit(50)
        ).scalars())
        for iq in inquiry_rows:
            s.expunge(iq)

    # 聚合 — 失败 fallback 已在 inquiry_group.aggregate 里做(每条单独成组)
    try:
        d.inquiry_groups = aggregate_inquiries(inquiry_rows)
    except Exception:  # noqa: BLE001
        log.exception("inquiry aggregate failed; fallback to flat list")
        d.inquiry_groups = [
            InquiryGroup(
                title="(独立追问)",
                master_question=(iq.question or "").strip(),
                member_ids=[iq.id],
                members=[iq],
            )
            for iq in inquiry_rows
        ]
    return d


def render_card(d: WeeklyDigest) -> str:
    """渲染周报。

    段编号:
        1-N 待沉淀(spec 候选)→ 回「批准 1-N」/「驳回 1-N」
        2-N 待修正(冲突)    → 回「采纳 2-N」(用新覆盖旧) / 「保留 2-N」(否决新)/ 「都留 2-N」(并存)
        3-N 待回答的追问主题  → 回「答 3-N 你的答案」(LLM 判答案覆盖了哪些子追问 → 全部一起 close)

    顶部摘要:
      本周新增 N 条判断
      memory_audit:
        - 首跑(dry-run)→ "本次预审 N 条 directive,M 条疑似误抽待确认。回「确认 audit」生效"
        - 自动模式 → "本次自动 supersede M 条误抽 directive"
    """
    lines = [
        f"📋 Helper 周报 ({d.week_start:%m-%d} ~ {d.week_end:%m-%d})",
        "",
        "💬 回执规则: 私聊 helper 时, 第一行写「周报裁判回执」, 后续每行一条指令。",
        "  没有这个开头的消息会被当闲聊处理, 不会绑到周报上。",
        "",
        f"本周新增 {d.raw_count} 条判断",
    ]
    # audit 摘要
    if d.audit is not None:
        ar = d.audit
        if ar.dry_run and ar.to_supersede:
            lines.append(
                f"🔍 memory_audit (首次预审): 审 {ar.audited} 条, "
                f"{len(ar.to_supersede)} 条疑似误抽待你确认 — 回「确认 audit」全部 supersede / "
                f"「跳过 audit」保留原状(后续每次自动跑 = 不再询问)"
            )
            for f in ar.to_supersede[:10]:
                scope = f"{f.scope_ref}" if f.scope_type == "entity" else "全局"
                lines.append(f"     · [{scope}] {f.directive[:80]}")
                lines.append(f"        理由: {f.reason[:100]}")
            if len(ar.to_supersede) > 10:
                lines.append(f"     ... 另 {len(ar.to_supersede) - 10} 条,「确认 audit」一并处理")
        elif not ar.dry_run and ar.to_supersede:
            lines.append(
                f"🔍 memory_audit: 自动 supersede {len(ar.to_supersede)} 条误抽 directive"
            )
        elif ar.audited and not ar.to_supersede:
            lines.append(f"🔍 memory_audit: 审 {ar.audited} 条 directive 全部通过")
    # inquiry_audit 摘要 (旧 prompt 留下的学究式追问自动 close)
    if d.inquiry_audit is not None:
        iar = d.inquiry_audit
        if iar.dropped:
            lines.append(
                f"🔍 inquiry_audit: 自动 close {len(iar.dropped)} 条按新判据失效的追问 "
                f"(审 {iar.audited} 条, 留 {iar.kept} 条真 G1/G2 缺口)"
            )
        elif iar.audited:
            lines.append(f"🔍 inquiry_audit: 审 {iar.audited} 条追问全部仍是 G1/G2 缺口, 保留")
    lines.append("")

    if d.pending_specs:
        lines.append(f"📝 1. 待沉淀规约 ({len(d.pending_specs)}) — 「批准 1-N」/「驳回 1-N」/「跳过 1-N」")
        for n, (_sid, _slug, title) in enumerate(d.pending_specs, start=1):
            lines.append(f"  1-{n}  {title}")
        lines.append("")
    if d.open_conflicts:
        lines.append(
            f"⚠️ 2. 待修正冲突 ({len(d.open_conflicts)}) — "
            "「采纳 2-N」新覆盖旧 / 「保留 2-N」否决新 / 「都留 2-N」并存"
        )
        for n, (_cid, ttype, _slug, _sev, summary) in enumerate(d.open_conflicts, start=1):
            type_label = {"spec": "规约", "memory": "偏好"}.get(ttype, ttype)
            lines.append(f"  2-{n}  [{type_label}] {summary}")
        lines.append("")
    if d.inquiry_groups:
        total_subs = sum(len(g.member_ids) for g in d.inquiry_groups)
        lines.append(
            f"❓ 3. 待回答的追问 ({len(d.inquiry_groups)} 个主题, 共 {total_subs} 条) — "
            "「3-N 你的答案」(覆盖到的子追问自动一起关闭) / 「3-N 展开说说」让 bot 给解释"
        )
        for n, g in enumerate(d.inquiry_groups, start=1):
            mq = g.master_question.replace("\n", " ").strip()
            if len(mq) > 120:
                mq = mq[:118] + "…"
            tag = f"[{g.title}]" if g.title and g.title != "(独立追问)" else ""
            covers = f"  (覆盖 {len(g.member_ids)} 条)" if len(g.member_ids) > 1 else ""
            lines.append(f"  3-{n}  {tag}{mq}{covers}")
        lines.append("")
    if not (d.pending_specs or d.open_conflicts or d.inquiry_groups
            or (d.audit and d.audit.to_supersede)):
        lines.append("✓ 本周 inbox 清空")
    return "\n".join(lines).rstrip()


def snapshot_digest(owner_domain: str, d: WeeklyDigest) -> None:
    """把一次周报的 1-N/2-N/3-N → 真实 ID 映射存进 inbox_digest。

    owner 一行,upsert。reply.py 用 owner 域账号反查最新 digest 解析。

    inquiries 字段语义升级: 现在是 list[list[int]] — 第 N-1 个 sublist 是 3-N
    主题对应的所有子追问 id 列表(可能 1 条 = 独立追问, 也可能 N 条 = 聚合组)。
    reply.py 把 owner 答案分发到所有子追问。

    pending_audit: 首次 audit dry-run 时, 把待 supersede 的 memory id 列表存这里,
    owner 回「确认 audit」时 reply.py 调 memory.apply_pending() 真生效。
    """
    if not owner_domain:
        return
    payload: dict = {
        "specs":     [sid for sid, _slug, _title in d.pending_specs],
        "conflicts": [cid for cid, *_ in d.open_conflicts],
        # 3-N → 该主题下所有子追问 id list (≥1)
        "inquiries": [list(g.member_ids) for g in d.inquiry_groups],
    }
    if d.audit is not None and d.audit.dry_run and d.audit.to_supersede:
        payload["pending_audit"] = [
            {
                "memory_id": f.memory_id,
                "directive": f.directive,
                "scope_type": f.scope_type,
                "scope_ref": f.scope_ref,
                "reason": f.reason,
            }
            for f in d.audit.to_supersede
        ]
    with session() as s:
        row = s.get(InboxDigest, owner_domain)
        if row is None:
            row = InboxDigest(
                owner_domain=owner_domain,
                items_json=json.dumps(payload, ensure_ascii=False),
                sent_at=datetime.now(timezone.utc),
            )
            s.add(row)
        else:
            row.items_json = json.dumps(payload, ensure_ascii=False)
            row.sent_at = datetime.now(timezone.utc)


def send_to(
    receiver_id: str,
    *,
    receiver_id_type: str = "user_id",
    owner_domain: str = "",
) -> bool:
    """构建当周 digest 并发出去。返成功与否。

    owner_domain 给 reply 解析用 — 没传就用 settings.helper_owner_domain。
    """
    from helper.config import get_settings
    if not owner_domain:
        owner_domain = get_settings().helper_owner_domain

    d = build_digest()
    body = render_card(d)
    try:
        wave_client.send_message(
            receiver_id,
            msg_type="text",
            content={"text": body},
            receiver_id_type=receiver_id_type,
            send_type=1,
        )
    except WaveAPIError as e:
        log.warning("weekly digest send failed → %s/%s: %s", receiver_id_type, receiver_id, e)
        return False
    snapshot_digest(owner_domain, d)
    return True
