"""消息意图分类 — 5 类语义意图 + unknown 兜底。

设计原则:
- 语义意图(judgment / ask / schedule_*)交给 LLM 判,不用字符 regex 猜语义
- 确定性的格式 / 状态信号(KM URL、/inbox、pending confirm)在 wave_actions 里前置处理,
  不进 intent.classify
- LLM 失败 / 输出无法解析 → unknown(不再默认 judgment),由调用方决定怎么兜
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from helper.llm import run

log = logging.getLogger(__name__)

Intent = Literal[
    "judgment", "ask", "schedule_create", "schedule_list", "schedule_cancel", "unknown"
]


SYSTEM_PROMPT = """你是消息意图分类器。判断用户在 IM 里 @bot 说的话属于哪一类:

- judgment: 用户在分享一段决策、判断、经验、结论、知识(陈述句、喂资料)。如"上周决定把 X 调到首页"、"这个文档是我们的可见性配置教程"
- ask: 用户在向 bot 提问期待回答。如"PRD 模板的风险章节应该放哪?"、"帮我总结下这文章"
- schedule_create: 用户想创建定时任务。如"每周一 9 点问我项目进展"、"每月 1 号给我发月报"
- schedule_list: 用户问当前有哪些定时任务。如"我有什么定时任务?"、"列出我的定时任务"
- schedule_cancel: 用户想取消定时任务,通常带 #编号 或"取消"二字。如"取消 #3"、"删掉那个周报"
- unknown: 闲聊、招呼、不可分类。如"你好"、"在吗"

只输出一个词: judgment / ask / schedule_create / schedule_list / schedule_cancel / unknown"""


_INTENT_RE = re.compile(
    r"\b(schedule_create|schedule_list|schedule_cancel|judgment|ask|unknown)\b",
    re.IGNORECASE,
)


def classify(text: str) -> Intent:
    """文本 → 意图。语义全交给 LLM,不做语义启发式。

    LLM 失败 / 解析失败 → unknown,由调用方决定怎么兜,不再悄悄默认成 judgment。
    """
    if not text or not text.strip():
        return "unknown"
    try:
        reply = run("intent_classify", system=SYSTEM_PROMPT, user=text, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed, default to unknown: %s", e)
        return "unknown"
    m = _INTENT_RE.search(reply.lower())
    if m:
        return m.group(1).lower()  # type: ignore[return-value]
    log.warning("intent classify output not parseable, default to unknown: %r", reply[:80])
    return "unknown"
