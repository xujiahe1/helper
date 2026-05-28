"""周报构建 + 投递。

数据源 (本周内):
- 新落库 raw 总数 + 出 L1 成功率
- pending review 的 spec_candidates
- open 冲突
- 未答的追问
- entity 候选数 + 已晋升数

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
from helper.storage import session
from helper.storage.models import (
    ConflictLog,
    EntityCandidate,
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
    # 列出未答追问全文 — 让 owner 直接看到要补什么。(id, raw_id, question)
    unanswered_inquiries: list[tuple[int, int, str]] = field(default_factory=list)
    entity_total: int = 0
    entity_promoted: int = 0


def _week_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, now


def build_digest() -> WeeklyDigest:
    start, end = _week_window()
    d = WeeklyDigest(week_start=start, week_end=end)
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

        inquiry_rows = s.execute(
            select(InquiryLog)
            .where(InquiryLog.created_at >= start)
            .where(InquiryLog.answer_raw_id.is_(None))
            .order_by(InquiryLog.created_at.desc())
            .limit(10)
        ).scalars().all()
        d.unanswered_inquiries = [(iq.id, iq.raw_id, iq.question) for iq in inquiry_rows]

        d.entity_total = len(s.execute(select(EntityCandidate.id)).scalars().all())
        d.entity_promoted = len(s.execute(
            select(EntityCandidate.id).where(EntityCandidate.promoted_at.is_not(None))
        ).scalars().all())
    return d


def render_card(d: WeeklyDigest) -> str:
    """渲染周报。三段编号:
        1-N 待沉淀(spec 候选)→ 回「批准 1-N」/「驳回 1-N」
        2-N 待修正(冲突)    → 回「采纳 2-N」(用新覆盖旧) / 「保留 2-N」(否决新)/ 「都留 2-N」(并存)
        3-N 待回答(追问)    → 回「答 3-N 你的答案」
    """
    lines = [
        f"📋 Helper 周报 ({d.week_start:%m-%d} ~ {d.week_end:%m-%d})",
        "",
        f"本周新增 {d.raw_count} 条判断 / L1 成功 {d.l1_ok} 失败 {d.l1_err}",
        f"Entity: 候选 {d.entity_total} 已晋升 {d.entity_promoted}",
        "",
    ]
    if d.pending_specs:
        lines.append(f"📝 1. 待沉淀规约 ({len(d.pending_specs)}) — 回「批准 1-N」/「驳回 1-N」")
        for n, (_sid, slug, title) in enumerate(d.pending_specs, start=1):
            lines.append(f"  1-{n}  {title}  〔{slug}〕")
        lines.append("")
    if d.open_conflicts:
        lines.append(
            f"⚠️ 2. 待修正冲突 ({len(d.open_conflicts)}) — "
            "回「采纳 2-N」用新覆盖旧 / 「保留 2-N」否决新 / 「都留 2-N」并存"
        )
        for n, (_cid, ttype, slug, sev, summary) in enumerate(d.open_conflicts, start=1):
            type_label = {
                "spec": "规约", "fact": "事实", "case": "案例",
                "concept": "概念", "relation": "关系",
            }.get(ttype, ttype)
            lines.append(f"  2-{n}  [{type_label}/{slug}] ({sev})")
            if summary:
                lines.append(f"        {summary}")
        lines.append("")
    if d.unanswered_inquiries:
        lines.append(f"❓ 3. 待回答的追问 ({len(d.unanswered_inquiries)}) — 回「答 3-N 你的答案」")
        for n, (_qid, rid, q) in enumerate(d.unanswered_inquiries, start=1):
            qline = q.replace("\n", " ").strip()
            if len(qline) > 80:
                qline = qline[:78] + "…"
            lines.append(f"  3-{n}  {qline}  〔raw#{rid}〕")
        lines.append("")
    if not (d.pending_specs or d.open_conflicts or d.unanswered_inquiries):
        lines.append("✓ 本周 inbox 清空")
    return "\n".join(lines).rstrip()


def snapshot_digest(owner_domain: str, d: WeeklyDigest) -> None:
    """把一次周报的 1-N/2-N/3-N → 真实 ID 映射存进 inbox_digest。

    owner 一行,upsert。reply.py 用 owner 域账号反查最新 digest 解析。
    """
    if not owner_domain:
        return
    payload = {
        "specs":     [sid for sid, _slug, _title in d.pending_specs],
        "conflicts": [cid for cid, *_ in d.open_conflicts],
        "inquiries": [qid for qid, _rid, _q in d.unanswered_inquiries],
    }
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
