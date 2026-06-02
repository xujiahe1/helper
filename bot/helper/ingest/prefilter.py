"""L1 预筛 — 群聊"听"路径专用,过滤掉无判断信号的闲聊。

为啥要预筛:bot 加群之后所有群消息都会落 raw_inputs 并默认全量跑 L1。
群消息绝大多数是闲聊 / 协作 ack,真正含决策信号的是少数(决定/方案/上线/...)。
全量跑 Sonnet L1 → token 烧穿;改成"关键词命中走完整 L1,边缘情况 mini 模型兜底"。

只用于群消息无差别 listen 路径。用户主动 @ bot 表态(judgment 路径)不预筛,
那是用户明确想沉淀一条判断,无脑跑 L1。

判断信号词来源: 项目 M4 计划里列的 6 类(决定 / 改成 / 延期 / 不做了 / 方案是 / 上线 / 选 X 不选 Y),
配上同义扩展。命中即视为高概率含决策,直接跑 L1,不再让 mini 判。

疑问句硬规则: 在关键词 / mini LLM 之前先判断是否疑问句, 命中即直接跳过。
背景: prompt v2 后 mini LLM 偶尔把 "刘佳翔就是哥吗?" 这种问句误判成 yes,
L1 抽成 section 进检索池。问句不管什么场景都不该进知识库。
"""

from __future__ import annotations

import logging
import re

from helper.llm import run

log = logging.getLogger(__name__)

# 命中即直接 L1 的关键词集合 — 中文表述常见的决策动词 / 状态变更 / 二选一句式
SIGNAL_KEYWORDS: tuple[str, ...] = (
    "决定", "决策", "敲定", "拍板", "拍了", "确认",
    "改成", "改为", "调整为", "改用",
    "延期", "推迟", "提前",
    "不做了", "砍掉", "下线", "暂缓", "搁置",
    "方案是", "方案定为", "最终方案",
    "上线", "灰度", "发布",
    "通过", "拒绝", "驳回",
    "选用", "采用",
)


def has_signal_keyword(text: str) -> bool:
    """关键词预筛 — O(N) 子串扫,毫秒级。"""
    if not text:
        return False
    return any(kw in text for kw in SIGNAL_KEYWORDS)


# 典型疑问句尾词 — 命中"问句结尾"模式:在末尾少量字符里出现 "X 吗" / "X 呢" / "怎么 X" 等。
# 不放在句首/中段是因为陈述句也常含 "为什么我们决定..." 这类词,只在末尾才有强信号。
_QUESTION_TAIL_RE = re.compile(
    r"(吗|呢|嘛)\s*[?？.。!!]*\s*$"
    r"|(怎么办|如何|为什么|是不是|能不能|可不可以|有没有|是吗)\s*[?？.。!!]*\s*$"
)
# 谁/什么/哪 等疑问代词在句首或紧跟空格后(陈述句里"什么是 X" 也算疑问)。
_QUESTION_HEAD_RE = re.compile(
    r"^\s*(谁|什么|哪|为啥|怎样|多少|几|何时|几时|啥)"
)


def is_question(text: str) -> bool:
    """判 text 是否疑问句。三档信号取或:
    1) 末尾问号(英/中文都算),最强信号
    2) 末尾出现"吗/呢/嘛"或典型疑问句尾词("是不是""怎么办""为什么"等)
    3) 句首疑问代词("谁""什么""哪""为啥""多少"…)
    """
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    # 1) 问号收尾(允许尾部有空白 / 表情)
    last = s.rstrip()[-1:] if s.rstrip() else ""
    if last in ("?", "?"):
        return True
    if _QUESTION_TAIL_RE.search(s):
        return True
    if _QUESTION_HEAD_RE.search(s):
        return True
    return False


_PREFILTER_PROMPT = """判断下面这句群聊消息是否包含"决策性内容"。
决策性内容 = 决定/选择/方案敲定/状态变更/原因解释/优先级判断/上线下线 等。
闲聊、问候、协作 ack(收到/好的/谢谢)、纯转发链接/文件、问问题(without 答案)— 都不算。

只输出一个字: y 或 n。不要别的字。"""


def llm_screen(text: str) -> bool:
    """让 mini 模型判断 yes/no。失败默认 yes(漏判比错过决策好)。"""
    if not text or not text.strip():
        return False
    try:
        reply = run(
            "l1_prefilter",
            system=_PREFILTER_PROMPT,
            user=text.strip()[:500],
            temperature=0.0,
            max_tokens=8,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("l1_prefilter llm failed, fallback yes: %s", e)
        return True
    answer = reply.strip().lower()
    return answer.startswith("y")


def should_run_l1(text: str) -> tuple[bool, str]:
    """返回 (是否跑 L1, 原因 tag)。原因 tag 用于 log 统计。

    顺序: 疑问句硬规则 → 关键词命中 → mini LLM 兜底。
    疑问句优先于关键词 — "决定不做了吗?" 这种含信号词的问句仍然不抽
    (问句永远不进知识库, L1 schema 也不接纳无答案的提问)。
    """
    if is_question(text):
        return False, "question"
    if has_signal_keyword(text):
        return True, "keyword"
    if llm_screen(text):
        return True, "llm_yes"
    return False, "llm_no"
