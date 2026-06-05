"""Inquiry 渲染聚合 — 周报第 3 段(待答追问)的合并层。

为什么需要:同一规约/同一实体下多条未答追问语义高度重叠(如鳕鱼老师授权
case 里 9 条追问都在问"边界 / 例外 / scope"),让 owner 一条条答既冗余又
让人放弃。聚合层把同主题追问合并成一条"总问题",owner 只需回答总问题。

这层只在周报渲染时跑(build_digest 后),不改 InquiryLog 表本身 — 原始追问
仍逐条留存,展开层供需要时查看。每次周报重建时重新跑(LLM 输出可能有
随机性,但 owner 看到的总问题应该相对稳定)。

聚合粒度: 全部送 LLM 自己分组 + 总结,不靠规则。原因:追问的"主题"不一定
能用 spec_slug / scope_ref 表达 — 比如鳕鱼老师授权的多条追问可能跨 raw,
共同主题是"鳕鱼老师授权解除的细节",不是某个 spec 的字段。

输出形态:
    [
      InquiryGroup(
        title="鳕鱼老师授权解除的边界",
        master_question="解除适用什么 scope?例外回到默认守口?",
        member_ids=[101, 102, 103, ...],
        members=[InquiryLog(...), ...],
      ),
      InquiryGroup(
        title="(独立追问)",
        master_question="<原问题>",
        member_ids=[107],
        members=[<那条 inquiry>],
      ),
      ...
    ]

独立追问(LLM 分组里只占一条)直接当一组 master 显示原问题,不调聚合
prompt — 省 LLM 调用。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from helper.llm import run
from helper.storage.models import InquiryLog

log = logging.getLogger(__name__)


@dataclass
class InquiryGroup:
    title: str                      # 给 owner 看的主题标签(短)
    master_question: str            # 合并后的总问题(owner 直接答这个)
    member_ids: list[int] = field(default_factory=list)
    members: list[InquiryLog] = field(default_factory=list)


# ── prompt 1: 分组 ─────────────────────────────────────────────────

_GROUP_SYSTEM_PROMPT = """你是追问分组员。

输入是 N 条未答追问。把语义同主题的归为一组(同一规约 / 同一实体 / 同一
事件下问"边界、例外、scope、永久性"等都算同主题)。每条追问只能进一组。
独自成主题(没有同主题伙伴)的就是单条组。

输出 JSON:
{
  "groups": [
    {"title": "<给 owner 看的主题标签, 中文, ≤ 12 字>", "ids": [<id 列表>]},
    ...
  ]
}

每组至少 1 条。所有输入 id 必须出现且仅出现一次。只输出 JSON,不要 markdown。"""


# ── prompt 2: 总问题合成 ───────────────────────────────────────────

_AGGREGATE_SYSTEM_PROMPT = """你是追问合并员。

输入是 N 条同主题的追问问题,把它们合并成 1 条**总问题** —
owner 答这一条总问题就能同时回应所有子问题。

要求:
- 总问题要包含所有子问题关心的维度(scope / 例外 / 永久性 / 触发条件 等),
  但用尽量自然的中文写,不堆术语
- 不要列举式 — 不要写"1) ... 2) ... 3) ...",写流畅的一两句话
- 总问题应该明显比每条子问题都"更高一层" — 子问题是这个总问题的具体侧面

只输出总问题正文(纯文本,不要 JSON,不要解释)。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_groups_json(text: str) -> list[dict] | None:
    text = (text or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    groups = data.get("groups") or []
    return groups if isinstance(groups, list) else None


def _llm_group(items: list[InquiryLog]) -> list[dict] | None:
    """让 LLM 把 inquiry 分组,返回 [{"title": ..., "ids": [...]}, ...]。失败返 None。"""
    if not items:
        return []
    if len(items) == 1:
        # 只有 1 条,跳过 LLM
        return [{"title": "(独立追问)", "ids": [items[0].id]}]
    refs = "\n".join(
        f"[id={iq.id}] {(iq.question or '').strip()[:200]}"
        for iq in items
    )
    user_msg = f"## 待分组的未答追问\n{refs}\n\n## 输出\nJSON。"
    try:
        reply = run("inquiry_aggregate", system=_GROUP_SYSTEM_PROMPT, user=user_msg, temperature=0.0)
    except Exception as e:  # noqa: BLE001
        log.warning("inquiry_aggregate (group) LLM failed: %s", e)
        return None
    return _parse_groups_json(reply)


def _llm_master_question(group_items: list[InquiryLog]) -> str:
    """合并组内追问为 1 个总问题。失败 fallback 拼第一条原文。"""
    if len(group_items) == 1:
        return (group_items[0].question or "").strip()
    refs = "\n".join(
        f"- {(iq.question or '').strip()}" for iq in group_items
    )
    user_msg = f"## 同主题子追问\n{refs}\n\n## 输出\n总问题正文。"
    try:
        reply = run(
            "inquiry_aggregate", system=_AGGREGATE_SYSTEM_PROMPT,
            user=user_msg, temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("inquiry_aggregate (master) LLM failed: %s", e)
        return (group_items[0].question or "").strip()
    return (reply or "").strip() or (group_items[0].question or "").strip()


# ── 入口 ────────────────────────────────────────────────────────────


def aggregate(items: list[InquiryLog]) -> list[InquiryGroup]:
    """把未答 inquiry 列表聚合成主题组。空列表返空。

    LLM 失败 fallback: 全部一条一组(原行为)。
    """
    if not items:
        return []

    by_id = {iq.id: iq for iq in items}
    groups_raw = _llm_group(items)
    if groups_raw is None:
        log.warning("inquiry aggregate fallback: each item as its own group")
        return [
            InquiryGroup(
                title="(独立追问)",
                master_question=(iq.question or "").strip(),
                member_ids=[iq.id],
                members=[iq],
            )
            for iq in items
        ]

    out: list[InquiryGroup] = []
    seen_ids: set[int] = set()
    for g in groups_raw:
        if not isinstance(g, dict):
            continue
        ids = g.get("ids") or []
        if not isinstance(ids, list):
            continue
        title = str(g.get("title", "")).strip() or "(无标题)"
        members: list[InquiryLog] = []
        for raw_id in ids:
            try:
                rid = int(raw_id)
            except (TypeError, ValueError):
                continue
            iq = by_id.get(rid)
            if iq is None or rid in seen_ids:
                continue
            seen_ids.add(rid)
            members.append(iq)
        if not members:
            continue
        master = _llm_master_question(members)
        out.append(InquiryGroup(
            title=title,
            master_question=master,
            member_ids=[m.id for m in members],
            members=members,
        ))

    # 兜底: 任何 LLM 漏掉的 id 单独成组
    for iq in items:
        if iq.id in seen_ids:
            continue
        log.warning("inquiry aggregate: LLM missed id=%d, fallback solo group", iq.id)
        out.append(InquiryGroup(
            title="(独立追问)",
            master_question=(iq.question or "").strip(),
            member_ids=[iq.id],
            members=[iq],
        ))
    return out
