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

# 启发式 — 命中即返,省一次 LLM(避免误伤的设计:只匹配"明显信号",其余兜给 LLM)
_CANCEL_RE = re.compile(r"取消\s*#?\d+|删掉.*定时|cancel\s+#?\d+", re.IGNORECASE)
_LIST_RE = re.compile(r"(我|当前|现在).*(定时任务|提醒).*(有|哪些|是什么)|列出.*定时|我的定时任务")

# ask 启发式:句尾问号 / 句尾"吗?"/"呢?"等语气助词 / "如何/怎么/为什么/什么是 X" 这种
# 显式疑问开头,且整句 ≤ 60 字符(长句容易夹陈述)。
_ASK_TAIL_QUESTION_PARTICLE_RE = re.compile(r"(吗|呢|嘛|么|不|没|呀)[?\s]*$")
_ASK_HEAD_RE = re.compile(
    r"^(请问|想问下?|想问问|麻烦问下|帮忙看看|有谁知道|你知道|"
    r"如何|怎么|怎样|为什么|为啥|什么是|啥是|哪些|哪个|哪种|多少|是不是|能不能|可不可以)"
)

# judgment 启发式:决策/事实陈述的明显标记 — 命中即跳 LLM
# 设计上保守,只命中"高置信"信号,其余还是走 LLM 兜底。
_JUDGMENT_SIGNAL_RE = re.compile(
    r"(决定|决策|认为|觉得|结论是|推荐|建议|应该|不应|"
    r"已经|已确认|证实|正确的是|错误的是|"
    r"是\s*\d|=\s*\S|的\s*(端口|地址|版本|型号|配置|参数|路径|名字)\s*是)"
)


def classify(text: str) -> Intent:
    """文本 → 意图。先走启发式(快、省 LLM),命中不到再走 LLM。

    误伤策略:启发式只命中"高置信信号",含糊不清的整句兜给 LLM。
    """
    if not text or not text.strip():
        return "other"
    s = text.strip()

    # schedule 三类(已有规则,留)
    if _CANCEL_RE.search(s):
        return "schedule_cancel"
    if _LIST_RE.search(s):
        return "schedule_list"

    # ask 信号 — 句尾问号 / 语气助词收尾 / 显式疑问词开头(限短句以免陈述里夹)
    if s.endswith(("?", "?")):
        return "ask"
    if _ASK_TAIL_QUESTION_PARTICLE_RE.search(s):
        return "ask"
    if len(s) <= 60 and _ASK_HEAD_RE.match(s):
        return "ask"

    # judgment 信号 — 短陈述 + 明显决策/事实标志,直接判 judgment
    if len(s) <= 200 and _JUDGMENT_SIGNAL_RE.search(s):
        return "judgment"

    try:
        reply = run("intent_classify", system=SYSTEM_PROMPT, user=text, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed, default to judgment: %s", e)
        return "judgment"
    m = _INTENT_RE.search(reply.lower())
    if m:
        return m.group(1).lower()  # type: ignore[return-value]
    return "judgment"
