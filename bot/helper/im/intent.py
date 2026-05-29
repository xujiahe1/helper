"""消息意图分类 — 5 类语义意图,ask 兜底。

设计原则:
- 语义意图(judgment / ask / schedule_*)交给 LLM 判,不用字符 regex 猜语义
- 确定性的格式 / 状态信号(KM URL、/inbox、pending confirm)在 wave_actions 里前置处理,
  不进 intent.classify
- 没有 unknown 类目: 用户 @bot 一定有诉求,边界情况(闲聊/解析失败/LLM 失败)
  统一兜底到 ask — 错判 ask 只是多查一次知识库无害, 错判 judgment 会把问句当资料喂 L1
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from helper.llm import run

log = logging.getLogger(__name__)

Intent = Literal[
    "judgment", "ask", "schedule_create", "schedule_list", "schedule_cancel"
]


SYSTEM_PROMPT = """你是消息意图分类器。判断用户在 IM 里 @bot 说的话属于哪一类:

- judgment: 用户在分享一段决策、判断、经验、结论、知识(陈述句、喂资料)。如"上周决定把 X 调到首页"、"这个文档是我们的可见性配置教程"
- ask: 用户在向 bot 提问期待回答,或闲聊招呼。如"PRD 模板的风险章节应该放哪?"、"帮我总结下这文章"、"哥是谁"、"你好"
- schedule_create: 用户想创建定时任务。如"每周一 9 点问我项目进展"、"每月 1 号给我发月报"
- schedule_list: 用户问当前有哪些定时任务。如"我有什么定时任务?"、"列出我的定时任务"
- schedule_cancel: 用户想取消定时任务,通常带 #编号 或"取消"二字。如"取消 #3"、"删掉那个周报"

如果消息附了「历史对话」段:仅用于解开当前消息里的指代/承接(如"再试一下"是接刚才那条),
不要让历史话题带偏当前意图判断。当前消息本身才是要分类的对象。

只输出一个词: judgment / ask / schedule_create / schedule_list / schedule_cancel"""


_INTENT_RE = re.compile(
    r"\b(schedule_create|schedule_list|schedule_cancel|judgment|ask)\b",
    re.IGNORECASE,
)


def classify(text: str, *, chat_context: str = "") -> Intent:
    """文本 → 意图。语义全交给 LLM,不做语义启发式。

    chat_context: 可选的历史对话标注块(已格式化好的字符串,空表示没有)。
    空文本 / LLM 失败 / 解析失败 → ask(让 ask runtime 兜,它有"无依据就说不知道"的低召回兜底)。
    """
    if not text or not text.strip():
        return "ask"
    if chat_context:
        user_msg = f"{chat_context}\n\n## 当前消息(请分类这条)\n{text}"
    else:
        user_msg = text
    try:
        reply = run("intent_classify", system=SYSTEM_PROMPT, user=user_msg, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed, default to ask: %s", e)
        return "ask"
    m = _INTENT_RE.search(reply.lower())
    if m:
        return m.group(1).lower()  # type: ignore[return-value]
    log.warning("intent classify output not parseable, default to ask: %r", reply[:80])
    return "ask"
