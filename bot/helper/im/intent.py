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


_BASE_HEAD = """你是消息意图分类器。判断用户在 IM 里 @bot 说的话属于哪一类。

判定核心是**意图方向**, 不是句式:

- judgment: 用户在**向 bot 灌信息** — 留下事实、决策、经验、规则、文档让 bot 记住或参考。
  本质是"用户 → bot"的单向输入, 用户不期待 bot 这次给出实质回答 (确认、致谢、寒暄式回应不算)。

- ask: 用户在**让 bot 产出** — 期待 bot 这一轮就给出答案、信息、判断、总结、列表、动作结果。
  无论是疑问句、祈使句、还是带陈述外壳的请求, 只要意图是要 bot 这次回点东西出来, 都是 ask。
  闲聊招呼也走 ask。

边界判定: 同一段话既灌信息又问问题 → 看**主诉求**是哪个, 主诉求是要 bot 答就 ask。 拿不准 → ask
(错判 ask 只是多查一次知识库无害, 错判 judgment 会把问句当资料喂 L1)。
"""

_SCHEDULE_BLOCK = """- schedule_create: 用户想创建定时任务。如"每周一 9 点问我项目进展"、"每月 1 号给我发月报"
- schedule_list: 用户问当前有哪些定时任务。如"我有什么定时任务?"、"列出我的定时任务"
- schedule_cancel: 用户想取消定时任务,通常带 #编号 或"取消"二字。如"取消 #3"、"删掉那个周报"
"""

_BASE_TAIL_FULL = """
如果消息附了「历史对话」段:仅用于解开当前消息里的指代/承接(如"再试一下"是接刚才那条),
不要让历史话题带偏当前意图判断。当前消息本身才是要分类的对象。

**历史延续陷阱(易误判 judgment)**: 前几条用户在向 bot 灌知识(说"记一下"/"重新记"/"补充..."等),
不代表当前这条还是在灌。话题相同、术语连续都不证明意图相同。
判定步骤: 把当前消息从历史里抽出来**单独看一遍** — 它本意是在告诉 bot 一件事(单向输出, 不期待答案),
还是在询问当前的状态/取值/做法/系统行为(陈述外壳, 本意要 bot 给答案)? 后者一律 ask, 哪怕话题紧接前文。

只输出一个词: judgment / ask / schedule_create / schedule_list / schedule_cancel"""

_BASE_TAIL_NO_SCHEDULE = """
如果消息附了「历史对话」段:仅用于解开当前消息里的指代/承接(如"再试一下"是接刚才那条),
不要让历史话题带偏当前意图判断。当前消息本身才是要分类的对象。

**历史延续陷阱(易误判 judgment)**: 前几条用户在向 bot 灌知识(说"记一下"/"重新记"/"补充..."等),
不代表当前这条还是在灌。话题相同、术语连续都不证明意图相同。
判定步骤: 把当前消息从历史里抽出来**单独看一遍** — 它本意是在告诉 bot 一件事(单向输出, 不期待答案),
还是在询问当前的状态/取值/做法/系统行为(陈述外壳, 本意要 bot 给答案)? 后者一律 ask, 哪怕话题紧接前文。

只输出一个词: judgment / ask"""

SYSTEM_PROMPT = _BASE_HEAD + _SCHEDULE_BLOCK + _BASE_TAIL_FULL
SYSTEM_PROMPT_NO_SCHEDULE = _BASE_HEAD + _BASE_TAIL_NO_SCHEDULE


_INTENT_RE = re.compile(
    r"\b(schedule_create|schedule_list|schedule_cancel|judgment|ask)\b",
    re.IGNORECASE,
)


def classify(text: str, *, chat_context: str = "", has_km_url: bool = False) -> Intent:
    """文本 → 意图。语义全交给 LLM,不做语义启发式。

    chat_context: 可选的历史对话标注块(已格式化好的字符串,空表示没有)。
    has_km_url: 当前消息含 KM URL 时为 True — 物理屏蔽 schedule_* 三类
      (含 URL 的消息只会是 judgment / ask,不会是定时任务)。这是确定性硬规则,
      避免历史对话里"重新学一下:URL"这种祈使句堆叠时 LLM 误判 schedule_create。
    空文本 / LLM 失败 / 解析失败 → ask(让 ask runtime 兜,它有"无依据就说不知道"的低召回兜底)。
    """
    if not text or not text.strip():
        return "ask"
    if chat_context:
        user_msg = f"{chat_context}\n\n## 当前消息(请分类这条)\n{text}"
    else:
        user_msg = text
    sys_prompt = SYSTEM_PROMPT_NO_SCHEDULE if has_km_url else SYSTEM_PROMPT
    try:
        reply = run("intent_classify", system=sys_prompt, user=user_msg, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed, default to ask: %s", e)
        return "ask"
    m = _INTENT_RE.search(reply.lower())
    if m:
        result = m.group(1).lower()
        if has_km_url and result.startswith("schedule_"):
            log.warning(
                "intent classify returned %s with KM URL — should never happen, fallback ask: %r",
                result, reply[:80],
            )
            return "ask"
        return result  # type: ignore[return-value]
    log.warning("intent classify output not parseable, default to ask: %r", reply[:80])
    return "ask"
