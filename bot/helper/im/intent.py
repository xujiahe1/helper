"""消息意图分类 — judgment / ask / schedule_create / schedule_list / schedule_cancel / other。

走 intent_classify(claude-sonnet-4-6)。schedule_* 三类是 M4 加的,bot 进程内
APScheduler 接管真实执行,intent.py 只做识别。
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from helper.llm import run

log = logging.getLogger(__name__)

Intent = Literal[
    "judgment", "ask", "schedule_create", "schedule_list", "schedule_cancel", "other"
]


SYSTEM_PROMPT = """你是消息意图分类器。判断用户在 IM 里 @bot 说的话属于哪一类:

- judgment: 用户在分享一段决策、判断、经验、结论(陈述句)。如"上周决定把 X 调到首页"
- ask: 用户在向 bot 提问期待回答。如"PRD 模板的风险章节应该放哪?"
- schedule_create: 用户想创建定时任务。如"每周一 9 点问我项目进展"、"每月 1 号给我发月报"
- schedule_list: 用户问当前有哪些定时任务。如"我有什么定时任务?"、"列出我的定时任务"
- schedule_cancel: 用户想取消定时任务,通常带 #编号 或"取消"二字。如"取消 #3"、"删掉那个周报"
- other: 闲聊、命令、不可分类。如"你好"

只输出一个词: judgment / ask / schedule_create / schedule_list / schedule_cancel / other"""


_INTENT_RE = re.compile(
    r"\b(schedule_create|schedule_list|schedule_cancel|judgment|ask|other)\b",
    re.IGNORECASE,
)

# 启发式 — 命中即返,省一次 LLM
_CANCEL_RE = re.compile(r"取消\s*#?\d+|删掉.*定时|cancel\s+#?\d+", re.IGNORECASE)
_LIST_RE = re.compile(r"(我|当前|现在).*(定时任务|提醒).*(有|哪些|是什么)|列出.*定时|我的定时任务")


def classify(text: str) -> Intent:
    """文本 → 意图。LLM 失败默认 judgment。"""
    if not text or not text.strip():
        return "other"
    s = text.strip()
    # 启发式快速通道
    if _CANCEL_RE.search(s):
        return "schedule_cancel"
    if _LIST_RE.search(s):
        return "schedule_list"
    if s.endswith(("?", "?")):
        return "ask"
    try:
        reply = run("intent_classify", system=SYSTEM_PROMPT, user=text, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed, default to judgment: %s", e)
        return "judgment"
    m = _INTENT_RE.search(reply.lower())
    if m:
        return m.group(1).lower()  # type: ignore[return-value]
    return "judgment"
