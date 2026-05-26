"""L1 结构化 — 把毛坯判断提取成"场景/信号/权衡/选择/原因"。

走 `claude-sonnet-4-6`(由 spec_repo/meta/policies/llm_routing.yaml 决定)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from helper.llm import run


@dataclass
class L1Structure:
    scene: str = ""
    signals: list[str] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)
    choice: str = ""
    rationale: str = ""
    raw_text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


SYSTEM_PROMPT = """你是一个决策结构化助手。

把用户给你的一段毛坯判断,提取成 5 个字段(JSON):
- scene: 这个判断在什么场景下做出?(一句话)
- signals: 看到了哪些信号 / 关键事实(字符串数组)
- tradeoffs: 权衡过哪些选项(字符串数组,每条说明该选项的利弊)
- choice: 最后选了什么(一句话)
- rationale: 为什么选这个不选其他(一句话)

只输出 JSON 对象,不要任何其他文字、不要 markdown 代码块。
如果原文中某字段无法判断,填空字符串 / 空数组,不要编造。"""


def structure(raw_text: str) -> L1Structure:
    """L1 结构化入口。失败时返回 error 字段,不抛异常。"""
    try:
        reply = run("l1_structure", system=SYSTEM_PROMPT, user=raw_text, temperature=0)
    except Exception as e:  # noqa: BLE001 — 任何 LLM/网络错误都该捕获,M2 加分级处理
        return L1Structure(raw_text=raw_text, error=f"LLM call failed: {type(e).__name__}: {e}")

    data = _parse_json(reply)
    if data is None:
        return L1Structure(
            raw_text=raw_text,
            error=f"bad JSON from LLM, first 200 chars: {reply[:200]!r}",
        )

    try:
        return L1Structure(
            scene=str(data.get("scene", "")),
            signals=[str(x) for x in data.get("signals", [])],
            tradeoffs=[str(x) for x in data.get("tradeoffs", [])],
            choice=str(data.get("choice", "")),
            rationale=str(data.get("rationale", "")),
            raw_text=raw_text,
        )
    except (TypeError, ValueError) as e:
        return L1Structure(raw_text=raw_text, error=f"parse error: {e}")


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict | None:
    """容忍 ```json``` 包裹 / 前后多余文字。"""
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None
