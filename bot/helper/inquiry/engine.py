"""追问 Engine — LLM 驱动版本(M2 灵魂模块)。

设计:
- 输入: raw + 该 raw 的 L1Item(只看 type=decision)+ 每个 decision 的 source_raw_ids
        对应的上下文 raw 文本 + 策略 yaml(全文喂给 LLM)
- LLM(走 elicit task,默认 Opus)自己判断哪些策略命中、写问题文本,返 0-N 条
- 引擎 cap top 3 by priority,落 inquiry_log

为啥不用 DSL 触发:边界判断本身就是细微判断,DSL 漏判多、误判多;策略 yaml 的
when 字段对 LLM 是语义说明,让 LLM 在抽象层判断"这条是否触发"更准。

幂等: 重跑前 DELETE inquiry_log WHERE raw_id=X AND answer_raw_id IS NULL AND hit='unknown'
      (已经被用户答过的保留作为审计)。

外部接口:
  generate_inquiries(raw_id) -> list[InquiryHit]   # 主入口,落 inquiry_log + 返新行
  evaluate_for_raw(raw_id)                         # 别名,向后兼容
  load_strategies()                                # spec_repo 读 yaml
  record_answer(inquiry_id, answer_raw_id)         # 用户回答 → 关联
  mark_hit(inquiry_id, 'yes'/'no'/'unknown')       # 命中率打标
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import and_, delete, select

from helper.config import get_settings
from helper.llm import run
from helper.storage import session
from helper.storage.models import InquiryLog, L1Item, RawInput

log = logging.getLogger(__name__)

INQUIRY_RELPATH = Path("meta") / "policies" / "inquiry_strategies.yaml"
_DEFAULT_PACKAGE = "helper.policy.defaults"
_DEFAULT_FILE = "inquiry_strategies.yaml"

_MAX_INQUIRIES_PER_RAW = 3
_MAX_CONTEXT_RAWS = 10  # source_raw_ids 引用的上下文最多注入这么多条,避免 prompt 爆炸


@dataclass
class InquiryHit:
    """生成的一条追问。raw_id=被追问的主消息,strategy_id=命中的策略,
    target_l1_idx=该追问针对哪个 L1Item(decision),priority 0-100。
    """

    raw_id: int
    strategy_id: str
    question: str
    target_l1_idx: int = 0
    priority: int = 50


# ─────────────────────── 策略加载 ───────────────────────

def load_strategies_text() -> str:
    """读 spec_repo 里的 yaml 原文(直接喂 LLM,不解析)。"""
    s = get_settings()
    f = s.helper_spec_git_dir / INQUIRY_RELPATH
    if f.exists():
        return f.read_text(encoding="utf-8")
    return (files(_DEFAULT_PACKAGE) / _DEFAULT_FILE).read_text(encoding="utf-8")


def load_strategies() -> list[dict[str, Any]]:
    """解析后的策略列表(供调用方查 priority / id 元数据)。"""
    text = load_strategies_text()
    data = yaml.safe_load(text) or {}
    strategies = data.get("strategies", [])
    return strategies if isinstance(strategies, list) else []


# ─────────────────────── prompt 构造 ───────────────────────

_SYSTEM_PROMPT = """你是"追问生成器" — 帮助专家把脑子里没说出口的边界条件、反例、隐含理由问出来。

输入会包含:
1. 主 raw 的全文 + 它抽出的 L1Item decision 数组(每条 decision 有完整 payload)
2. 决策引用的上下文 raw 文本(decision payload 里 source_raw_ids 指向的)
3. 追问策略目录(yaml,每条策略有 id / priority / when / why / question_hint)

你的工作:
- 对每条 decision 扫所有策略,判断哪些**真的命中**(根据 when 的语义说明,不是关键词)
- 命中的策略 → **根据具体场景写一个问题**(参考 question_hint 但不要照抄)
- 不要拼凑模板,要让问题文本贴切到这条决策的内容
- 全局只输出 priority 最高的 3 条,其他丢弃
- 一条 decision 至少 0 条至多 2 条追问;同一 decision 不要触发多个相似策略

输出 JSON 数组,每个元素:
{
  "strategy_id": "<必须是策略 yaml 里的 id>",
  "question": "<给用户看的具体问题文本,中文,可多行;用选择题/反例追问/量化追问形态>",
  "target_l1_idx": <decision 在 L1Item 数组里的 idx,从 0 开始>,
  "priority": <整数,从策略 yaml 抄>
}

硬性要求:
- 不要重复 rationale 已经说清楚的内容(不问"你为什么选 X" — 用户已经在 rationale 里答了)
- 不要问空话(不问"你确定吗"、"还有补充吗")
- 不要编造 raw / 上下文里没有的信息
- 不要 markdown 代码块,直接输出 JSON 数组。0 条追问输出 [] 即可。"""


def _format_l1_decisions(items: list[L1Item]) -> str:
    """把 type=decision 的 L1Item 渲染成 prompt 用的列表。"""
    lines = []
    for it in items:
        if it.type != "decision":
            continue
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        lines.append(f"### L1Item idx={it.idx} (type=decision)")
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n".join(lines) if lines else "(无 decision 原子)"


def _collect_context_raw_ids(items: list[L1Item], primary_raw_id: int) -> list[int]:
    """从 decision payload.source_raw_ids 收齐去重的上下文 raw_id(排除主 raw)。"""
    seen: set[int] = set()
    out: list[int] = []
    for it in items:
        if it.type != "decision":
            continue
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        src = payload.get("source_raw_ids") or []
        if not isinstance(src, list):
            continue
        for rid in src:
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue
            if rid_int == primary_raw_id or rid_int in seen:
                continue
            seen.add(rid_int)
            out.append(rid_int)
            if len(out) >= _MAX_CONTEXT_RAWS:
                return out
    return out


def _format_context_block(context_rows: list[RawInput]) -> str:
    if not context_rows:
        return "(无)"
    lines = []
    for r in context_rows:
        speaker = r.author_domain or "user"
        ts = r.created_at.strftime("%H:%M") if r.created_at else ""
        text = (r.content_text or "").strip().replace("\n", " ")
        lines.append(f"[raw#{r.id} @{speaker} {ts}] {text}")
    return "\n".join(lines)


def _build_user_prompt(
    raw: RawInput,
    decision_items: list[L1Item],
    context_rows: list[RawInput],
    strategies_text: str,
) -> str:
    speaker = raw.author_domain or "user"
    return (
        "## 追问策略目录(yaml)\n"
        "```yaml\n"
        f"{strategies_text.strip()}\n"
        "```\n\n"
        "## 主 raw\n"
        f"[raw#{raw.id} @{speaker}] {(raw.content_text or '').strip()}\n\n"
        "## 主 raw 抽出的 decision L1Item\n"
        f"{_format_l1_decisions(decision_items)}\n\n"
        "## 上下文 raw(主 decision 的 source_raw_ids 引用)\n"
        f"{_format_context_block(context_rows)}\n\n"
        "## 输出\n"
        "JSON 数组。无追问输出 []。"
    )


# ─────────────────────── 解析 LLM 输出 ───────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json_array(text: str) -> list | None:
    text = (text or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, list) else None


def _normalize_hits(
    raw_arr: list,
    *,
    raw_id: int,
    valid_strategy_ids: set[str],
    strategy_priority: dict[str, int],
) -> list[InquiryHit]:
    """LLM 输出 → InquiryHit 列表;过滤未知 strategy_id;cap top 3 by priority。"""
    out: list[InquiryHit] = []
    for item in raw_arr:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("strategy_id", "")).strip()
        question = str(item.get("question", "")).strip()
        if not sid or not question:
            continue
        if sid not in valid_strategy_ids:
            log.debug("inquiry: unknown strategy_id %r dropped", sid)
            continue
        # 优先用 LLM 输出的 priority,否则回退 yaml 元数据;再否则 50
        try:
            prio = int(item.get("priority"))
        except (TypeError, ValueError):
            prio = strategy_priority.get(sid, 50)
        try:
            target_idx = int(item.get("target_l1_idx", 0))
        except (TypeError, ValueError):
            target_idx = 0
        out.append(InquiryHit(
            raw_id=raw_id,
            strategy_id=sid,
            question=question,
            target_l1_idx=target_idx,
            priority=prio,
        ))
    out.sort(key=lambda h: h.priority, reverse=True)
    return out[:_MAX_INQUIRIES_PER_RAW]


# ─────────────────────── 主入口 ───────────────────────

def generate_inquiries(raw_id: int) -> list[InquiryHit]:
    """对 raw_id 跑一次追问生成。返回新写入的 InquiryHit 列表(可能为空)。

    幂等: 重跑前清掉同 raw 下未被回答的旧追问,新生成的覆盖写入。
    """
    strategies = load_strategies()
    if not strategies:
        log.info("inquiry: no strategies loaded, skip raw#%d", raw_id)
        return []
    valid_ids = {str(st.get("id")) for st in strategies if st.get("id")}
    priority_map = {
        str(st.get("id")): int(st.get("priority", 50))
        for st in strategies
        if st.get("id")
    }
    strategies_text = load_strategies_text()

    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return []
        l1_items = list(
            s.execute(
                select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
            ).scalars()
        )
        decision_items = [it for it in l1_items if it.type == "decision"]
        if not decision_items:
            log.debug("inquiry: raw#%d has no decision atoms, skip", raw_id)
            return []
        # 把 ORM 行从 session 摘出来,避免后续读时 expired
        for it in decision_items:
            s.expunge(it)
        s.expunge(raw)

    ctx_ids = _collect_context_raw_ids(decision_items, primary_raw_id=raw_id)
    with session() as s:
        if ctx_ids:
            ctx_rows = list(
                s.execute(
                    select(RawInput).where(RawInput.id.in_(ctx_ids)).order_by(RawInput.id)
                ).scalars()
            )
            for r in ctx_rows:
                s.expunge(r)
        else:
            ctx_rows = []

    user_prompt = _build_user_prompt(raw, decision_items, ctx_rows, strategies_text)
    try:
        reply = run("elicit", system=_SYSTEM_PROMPT, user=user_prompt)
    except Exception as e:  # noqa: BLE001
        log.warning("inquiry LLM failed raw#%d: %s", raw_id, e)
        return []

    arr = _parse_json_array(reply)
    if arr is None:
        log.warning("inquiry: bad JSON from LLM raw#%d, first 200 chars: %r", raw_id, reply[:200])
        return []

    hits = _normalize_hits(
        arr,
        raw_id=raw_id,
        valid_strategy_ids=valid_ids,
        strategy_priority=priority_map,
    )

    # 幂等:清未答的旧 inquiry,再插新的
    with session() as s:
        s.execute(
            delete(InquiryLog).where(
                and_(
                    InquiryLog.raw_id == raw_id,
                    InquiryLog.answer_raw_id.is_(None),
                    InquiryLog.hit == "unknown",
                )
            )
        )
        for h in hits:
            s.add(InquiryLog(
                raw_id=h.raw_id,
                strategy_id=h.strategy_id,
                question=h.question,
            ))
        s.commit()

    log.info("inquiry: raw#%d → %d question(s) [%s]",
             raw_id, len(hits), ", ".join(h.strategy_id for h in hits))
    return hits


# 向后兼容别名
def evaluate_for_raw(raw_id: int) -> list[InquiryHit]:
    return generate_inquiries(raw_id)


def record_answer(inquiry_id: int, answer_raw_id: int) -> None:
    """用户回答了某次追问 — 把回答的 raw_id 关联回去。"""
    with session() as s:
        row = s.get(InquiryLog, inquiry_id)
        if row is None:
            return
        row.answer_raw_id = answer_raw_id
        s.commit()


def mark_hit(inquiry_id: int, hit: str) -> None:
    """标记本次追问是否答到点(yes/no/unknown)。"""
    if hit not in ("yes", "no", "unknown"):
        raise ValueError(f"hit must be yes/no/unknown, got {hit!r}")
    with session() as s:
        row = s.get(InquiryLog, inquiry_id)
        if row is None:
            return
        row.hit = hit
        s.commit()
