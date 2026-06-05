"""Memory audit — 周报/inbox 触发前的存量 directive 复审。

为什么需要:memory_extract 是入库时单 LLM 调用,边界判得不稳,会把"X 是 Y"
身份纠正、"X 是 Z,记住"评价陈述、"X 不能 Y,因为 Z"决策依据这类**知识/事实**
误抽成 directive。这些误抽 directive 会:
1. 进 ask 的 SYSTEM_PROMPT 用户偏好段污染答题
2. 互撞产生 memory 冲突,堆到 inbox 折磨 owner

audit 是 inbox/weekly.build_digest() 的前置步骤,逐条 LLM judge 现存 alive memory
究竟是真 directive 还是误抽,误抽的自动 supersede 掉。

节流:每条 alive memory 7 天内最多审一次(`last_audited_at` 列)。第一次跑会
全扫(节流不命中),之后每次只扫新加的 + 上次审 7 天前的。

dry_run vs apply:
- 第一次跑(Memory 表里从未有过 superseded_by=0 的行)→ dry_run=True
  误抽列表挂到 inbox_digest.pending_audit,owner 回「确认 audit」/「跳过 audit」
  决定要不要 apply。这是首次保护,避免一上来就批量改库
- 之后跑 → dry_run=False,直接 supersede,周报只显示摘要
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from helper.llm import run
from helper.storage import session
from helper.storage.models import ConflictLog, Memory

log = logging.getLogger(__name__)


_AUDIT_THROTTLE = timedelta(days=7)
# 单次最多审多少条 — 防 LLM 调用爆炸,首跑全表 100+ 条也能在 1-2 分钟内跑完。
# 没审完的下次再跑(last_audited_at 仍为 NULL,优先级最高)。
_MAX_AUDIT_PER_RUN = 100


SYSTEM_PROMPT = """你是 memory directive 审计员。

# 背景
helper 这个 bot 有两个并行的知识层:
1. **L1 语义原子**(section/decision)— 描述客观世界的事实、决策依据、定义、属性
2. **procedural memory(directive)** — 用户对 bot 行为的指令(怎么答、避谈、复述、路由 等)

入库时一条新内容会同时跑 L1 抽取和 memory 抽取。memory 抽取**容易把知识/事实
误判成 directive**,需要你来甄别已经入库的存量 memory。

# 任务
给你一条 directive 文本,判断它是不是真行为指令。

# 真 directive(保留)— 必须满足:
1. **执行动作的主语是 bot**(回答/复述/避谈/路由/称呼/格式/风格等)
2. **约束的是 bot 怎么呈现答案**,不是知识层判断

例:
- "答哥相关的问题别每次复述身份" → 真 directive(动作=答,主语=bot)
- "涉及 X 类问题路由给 @tachi" → 真 directive(动作=路由)
- "鳕鱼老师不参与哥相关话题,如有人问别提及" → 真 directive(动作=别提及)

# 误抽(应被 supersede)— 任一命中即误抽:

(A) **身份纠正/认领**:"X 是 Y / X 不是 Z / X 是 Y 不是 W"
    动作主语不是 bot,改的是世界认知,这是 fact。
    例:"小猫老师是周婷,不是陈雨晴"

(B) **属性/评价陈述 + 祈使外壳**:"X 是 Z,记住这一点 / 你要知道 X 很 Y"
    "记住"/"你要知道"是语气词,本体仍是客观陈述。
    例:"小猫老师是好人,记住这一点"

(C) **决策依据/技术原理**:"X 不能 Y,因为 Z / A 应该 B,原因是 C"
    这是工程/业务决策的"场景→选择→理由"结构,属 L1 decision 范畴。
    例:"北杨外包门禁不能直接设为工位楼层,因为 ..."

(D) **替换测试**:把句子改成"X 是 / X 不是 / X 应该 / X 因为"等陈述形态,
    若语义不变 → 是知识/事实/决策依据,误抽
    若语义只剩客观描述、丢失了"bot 怎么做"的部分 → 真 directive

# 输出 JSON

{
  "is_directive": true | false,
  "reason": "如果 false,一句话说明命中哪条(A/B/C/D);如果 true,一句话说明动作主语和 bot 行为约束"
}

只输出 JSON,不要 markdown 包裹。"""


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


@dataclass
class AuditFinding:
    memory_id: int
    directive: str
    scope_type: str
    scope_ref: str
    reason: str


@dataclass
class AuditReport:
    audited: int = 0       # 本次实际跑 LLM judge 的条数
    kept: int = 0          # 判为真 directive 保留的
    to_supersede: list[AuditFinding] = field(default_factory=list)
    failed: int = 0        # LLM 报错跳过的(下次再来)
    dry_run: bool = False  # True = 没真改 superseded,挂 pending_audit 等 owner 确认


def _is_first_run() -> bool:
    """判定首次 audit:Memory 表里有没有过 superseded_by=0 的行(audit 自动 supersede 标志)。

    人工裁决/conflict resolve 的 superseded_by 永远指向真实 raw_id(>0),
    所以 superseded_by=0 是 audit 留下的唯一指纹。一旦有过一次 apply,后续转入自动模式。
    """
    with session() as s:
        row = s.execute(
            select(Memory.id).where(Memory.superseded_by == 0).limit(1)
        ).scalar_one_or_none()
    return row is None


def _select_alive_to_audit() -> list[Memory]:
    """拉所有 alive 且 7 天内没审过的 memory。"""
    cutoff = datetime.now(timezone.utc) - _AUDIT_THROTTLE
    with session() as s:
        rows = list(s.execute(
            select(Memory)
            .where(Memory.superseded_at.is_(None))
            .where(or_(
                Memory.last_audited_at.is_(None),
                Memory.last_audited_at < cutoff,
            ))
            .order_by(Memory.last_audited_at.is_(None).desc(), Memory.id)
            .limit(_MAX_AUDIT_PER_RUN)
        ).scalars())
        for r in rows:
            s.expunge(r)
    return rows


def _judge_one(directive: str) -> dict | None:
    user_msg = f"## 待审 directive\n{directive.strip()}\n\n## 输出\nJSON。"
    try:
        reply = run(
            "memory_audit",
            system=SYSTEM_PROMPT,
            user=user_msg,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("memory_audit LLM failed directive=%r: %s", directive[:60], e)
        return None
    return _parse_json(reply)


def _mark_audited(memory_id: int) -> None:
    """单纯更新 last_audited_at — judge 真 directive 时调用。"""
    now = datetime.now(timezone.utc)
    with session() as s:
        m = s.get(Memory, memory_id)
        if m is None:
            return
        m.last_audited_at = now


def _apply_supersede(memory_id: int) -> None:
    """audit 自动 supersede — 打 superseded_at + superseded_by=0(audit 指纹)。

    级联: 同一 memory 是 ConflictLog.target 的 open 行也要标为 auto_rejected
    (这条 memory 被 audit 判误抽了, 它和别的 memory 之间的"冲突"自然作废)。
    旧 ConflictLog 留着是 owner 体感 bug 的根源 — 周报会一直把它当 2-N 显示。
    """
    now = datetime.now(timezone.utc)
    with session() as s:
        m = s.get(Memory, memory_id)
        if m is None or m.superseded_at is not None:
            return
        m.superseded_at = now
        m.superseded_by = 0
        m.last_audited_at = now

        # 关掉所有引用该 memory 的 open 冲突 — 两个方向都要清:
        # (1) target 端: memory#X 是被冲突的旧 directive(target_slug=str(X))
        # (2) source 端: memory#X 是新指令(extract.py 的 summary 形如
        #     "新指令(memory#{X}: ...)与已有 memory#{old}"), audit 把新指令
        #     判误抽时, 这条 conflict 也作废。用 summary like 'memory#X' 做软匹配
        #     — extract.py 里 summary 拼的形态是稳定的。
        marker = f"memory#{memory_id}"
        cl_rows = s.execute(
            select(ConflictLog)
            .where(ConflictLog.resolution == "open")
            .where(ConflictLog.target_type == "memory")
        ).scalars().all()
        for cl in cl_rows:
            if cl.target_slug == str(memory_id) or marker in (cl.summary or ""):
                cl.resolution = "auto_rejected"
                cl.resolved_by = "memory_audit"
                cl.resolved_at = now
                cl.auto_reason = "memory_audit 判定该 memory 误抽,自动作废相关冲突"


def _cleanup_dangling_memory_conflicts() -> int:
    """扫所有 open + target_type=memory 的 conflict_log, 把 target memory 已 dead 的全部 auto_rejected。

    覆盖**历史残留**: 不管 memory 是被哪条路径 supersede 的(audit / 人工裁决 / 旧 ingest 路径),
    只要现在已 dead,引用它的 open conflict 一律作废。修早期 audit 上线前/外路径 supersede
    的 memory 留下的僵尸 conflict —— 不修周报会一直把它们当 2-N 显示骚扰 owner。

    注: _apply_supersede 内的清理只清"本次 supersede 的这条 memory 涉及的 conflict",
    不覆盖外路径。这函数是兜底全扫。
    """
    now = datetime.now(timezone.utc)
    fixed = 0
    with session() as s:
        rows = list(s.execute(
            select(ConflictLog)
            .where(ConflictLog.resolution == "open")
            .where(ConflictLog.target_type == "memory")
        ).scalars())
        for cl in rows:
            try:
                mid = int(cl.target_slug)
            except (ValueError, TypeError):
                continue
            mem = s.get(Memory, mid)
            if mem is None or mem.superseded_at is None:
                continue
            cl.resolution = "auto_rejected"
            cl.resolved_by = "memory_audit"
            cl.resolved_at = now
            cl.auto_reason = "target memory 已 supersede, 作废残留 conflict"
            fixed += 1
    if fixed:
        log.info("memory_audit cleanup: auto_rejected %d dangling memory conflicts", fixed)
    return fixed


def run_audit() -> AuditReport:
    """跑一轮 audit。第一次 dry_run,后续 apply。

    调用方:inbox.weekly.build_digest() 起头跑这个。返回的 report 用于:
    - dry_run=True:weekly 把 to_supersede 写进 inbox_digest.pending_audit
    - dry_run=False:supersede 已立即生效,weekly 只渲染 "本周自动 supersede N 条" 摘要
    """
    # 不论 dry_run / apply, 先扫一遍历史残留 conflict — 覆盖外路径 supersede 留下的僵尸。
    _cleanup_dangling_memory_conflicts()

    dry_run = _is_first_run()
    rows = _select_alive_to_audit()
    report = AuditReport(dry_run=dry_run)

    if not rows:
        log.info("memory_audit: no rows due for audit (throttle 7d), skip")
        return report

    log.info("memory_audit: dry_run=%s, %d row(s) to audit", dry_run, len(rows))

    for m in rows:
        judged = _judge_one(m.directive or "")
        if judged is None:
            report.failed += 1
            # 失败不更新 last_audited_at — 下次再来(选项 c)
            continue
        report.audited += 1
        is_dir = bool(judged.get("is_directive"))
        reason = str(judged.get("reason", "")).strip()

        if is_dir:
            report.kept += 1
            _mark_audited(m.id)
            continue

        # 误抽
        finding = AuditFinding(
            memory_id=m.id,
            directive=m.directive or "",
            scope_type=m.scope_type or "global",
            scope_ref=m.scope_ref or "",
            reason=reason or "(LLM 未给理由)",
        )
        report.to_supersede.append(finding)

        if dry_run:
            # dry-run: 标记已审,但不真 supersede
            _mark_audited(m.id)
        else:
            _apply_supersede(m.id)

    log.info(
        "memory_audit done: audited=%d kept=%d to_supersede=%d failed=%d dry_run=%s",
        report.audited, report.kept, len(report.to_supersede),
        report.failed, dry_run,
    )
    return report


def apply_pending(findings: list[dict]) -> int:
    """owner 回「确认 audit」时调用,把 pending_audit 列表里的 memory 全部真 supersede。

    findings 是 inbox_digest.pending_audit 反序列化出来的 list[dict]。返回成功 supersede 的条数。
    """
    n = 0
    for f in findings:
        try:
            mid = int(f.get("memory_id", 0))
        except (TypeError, ValueError):
            continue
        if not mid:
            continue
        _apply_supersede(mid)
        n += 1
    log.info("memory_audit apply_pending: superseded %d memory row(s)", n)
    return n
