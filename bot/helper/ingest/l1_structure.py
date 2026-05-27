"""L1 抽取 — 把毛坯输入(聊天 / 文档 / 任意文本)抽成 0..N 条**知识原子**。

知识原子 5 类(全平等,不预设哪类多 / 哪类必须有):
- decision: 一次决策判断。{scene, signals[], tradeoffs[], choice, rationale}
- fact:    决策性事实(主谓宾)。{subject, predicate, object, scope}
- case:    具体案例 / 反例(发生过什么)。{scene, what_happened, outcome, referenced_spec?}
- concept: 核心概念 / 实体定义。{name, entity_type, description}
- relation: 实体间关系。{entity_a, relation, entity_b, description?}

走 `claude-sonnet-4-6`(由 spec_repo/meta/policies/llm_routing.yaml 决定)。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from helper.llm import run

log = logging.getLogger(__name__)

ALLOWED_TYPES = {"decision", "fact", "case", "concept", "relation"}


@dataclass
class L1Item:
    """单条知识原子(L1Result 表的一行)。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class L1Output:
    """一次 L1 抽取的整体结果。"""

    items: list[L1Item] = field(default_factory=list)
    raw_text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


SYSTEM_PROMPT = """你是知识原子抽取器。给你一段任意文本(可能是聊天片段,也可能是规章文档),
你要从中抽出**所有**值得沉淀的"知识原子",每条原子归到下列 5 类之一。

输入可能是两种形态:
A) 一段独立文本 — 直接抽。
B) 群聊 @bot 触发,带"## 上下文" + "## 主消息" 两块:
   - 主消息是被 @bot 的那条,通常很短(如"@bot prd模板风险章节放前面吧")
   - 上下文是同一对话最近 N 分钟的消息(每行带 [raw#ID @speaker] 前缀)
   - 抽取时把主消息看作"决策时刻",**上下文是支撑这个决策的素材** —
     主消息里没说全的 scene / signals / rationale,**应当从上下文补齐**。
   - 同时记录信息源:见下方 source_raw_ids / decision_speaker / rationale_speaker。

类型与字段:
1. decision — 某场景下做出的判断。
   字段: scene / signals(数组) / tradeoffs(数组) / choice / rationale
   附加(群聊场景填,独立文本可空):
     source_raw_ids: [raw_id, ...]  本条 decision 引用了哪些 raw(主 + 上下文)
     primary_raw_id: int             主消息 raw_id(决策本身这一条)
     decision_speaker: str           主消息说话人(domain / union_id / id 都可)
     rationale_speaker: str          rationale 主要来自谁(可能 ≠ decision_speaker)
2. fact — 静态可验证事实(主谓宾)。
   字段: subject / predicate / object / scope(可空)
   附加: source_raw_ids(可选)
3. case — 发生过的案例 / 反例。
   字段: scene / what_happened / outcome / referenced_spec(可空)
   附加: source_raw_ids(可选)
4. concept — 术语 / 概念 / 实体定义。
   字段: name / entity_type / description
5. relation — 实体间关系。
   字段: entity_a / relation / entity_b / description(可空)

输出 JSON 数组,每个元素 {"type": "<五者之一>", ...该 type 的字段}。

硬性要求:
- 只抽文本里直接出现 / 直接可推导的内容,不编造。
- 抽多少条由文本含量决定:0 / 1 / 几十都可,不要用类型预设条数。
- 群聊场景下,**主消息是决策核心**,如果上下文里也有独立的判断/事实/反例(不是为
  主消息服务的素材),也分别抽出来 — 一次抽完所有原子。
- 同一原子重复提及只抽一次。
- 群聊 decision 的 source_raw_ids 要把"被引用作 signal/rationale 的上下文 raw_id"
  也列出来,不能只填主消息。
- 直接输出 JSON 数组,不要解释、不要代码块。空 → []。"""


def _format_context_block(context: list[dict] | None) -> str:
    """把上下文行渲染成 prompt 用的 [raw#ID @speaker] text 列表。

    context 期望是按时间正序的 [{raw_id, speaker, text, ts}]。
    """
    if not context:
        return ""
    lines = []
    for c in context:
        raw_id = c.get("raw_id", "?")
        speaker = c.get("speaker") or "user"
        ts = c.get("ts", "")
        text = (c.get("text") or "").strip().replace("\n", " ")
        prefix = f"[raw#{raw_id} @{speaker}"
        if ts:
            prefix += f" {ts}"
        prefix += "]"
        lines.append(f"{prefix} {text}")
    return "\n".join(lines)


def _build_user_prompt(
    raw_text: str,
    *,
    context: list[dict] | None,
    primary_raw_id: int | None,
    primary_speaker: str,
) -> str:
    """有 context 时拼 ## 上下文 + ## 主消息 两块;无 context 时直接传原文。"""
    if not context:
        return raw_text
    ctx_block = _format_context_block(context)
    primary_prefix = "[主消息"
    if primary_raw_id is not None:
        primary_prefix += f" raw#{primary_raw_id}"
    if primary_speaker:
        primary_prefix += f" @{primary_speaker}"
    primary_prefix += "]"
    return (
        "## 上下文(同对话最近窗口,按时间正序)\n"
        f"{ctx_block}\n\n"
        "## 主消息(本次 @bot 触发抽取)\n"
        f"{primary_prefix} {raw_text.strip()}"
    )


def structure(
    raw_text: str,
    *,
    context: list[dict] | None = None,
    primary_raw_id: int | None = None,
    primary_speaker: str = "",
) -> L1Output:
    """L1 入口。LLM/解析失败时返回 error 字段非空,不抛。

    群聊 @bot 路径传 context = [{raw_id, speaker, text, ts}, ...] 把上下文窗口
    一并喂给 LLM,让它从上下文补齐主消息缺失的 scene/signals/rationale。
    """
    if not (raw_text or "").strip():
        return L1Output(raw_text=raw_text)
    user_prompt = _build_user_prompt(
        raw_text,
        context=context,
        primary_raw_id=primary_raw_id,
        primary_speaker=primary_speaker,
    )
    try:
        reply = run("l1_structure", system=SYSTEM_PROMPT, user=user_prompt, temperature=0)
    except Exception as e:  # noqa: BLE001 — 任何 LLM/网络错误都该捕获
        return L1Output(raw_text=raw_text, error=f"LLM call failed: {type(e).__name__}: {e}")

    arr = _parse_json_array(reply)
    if arr is None:
        return L1Output(
            raw_text=raw_text,
            error=f"bad JSON from LLM, first 200 chars: {reply[:200]!r}",
        )

    items: list[L1Item] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "")).strip().lower()
        if t not in ALLOWED_TYPES:
            log.debug("l1: unknown type %r dropped", t)
            continue
        payload = {k: v for k, v in item.items() if k != "type"}
        items.append(L1Item(type=t, payload=payload))
    return L1Output(items=items, raw_text=raw_text)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json_array(text: str) -> list | None:
    """容忍 ```json``` 包裹 / 前后多余文字。空数组合法。"""
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
