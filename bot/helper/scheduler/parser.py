"""LLM 解析自然语言 → ParsedSchedule(cron_expr + task_type + params + summary)。

LLM 输出 JSON 后,本地用 APScheduler CronTrigger.from_crontab 二次校验 cron 合法性。
不合法直接返 None,让上层提示用户重描述,不要往 DB 落坏数据。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from apscheduler.triggers.cron import CronTrigger

from helper.llm import run

log = logging.getLogger(__name__)


@dataclass
class ParsedSchedule:
    cron_expr: str           # "min hour dom mon dow"
    task_type: str           # periodic_ask / ...
    params: dict             # 类型相关参数,如 {"question": "..."}
    summary: str             # 给用户看的人类可读描述


_SYSTEM_PROMPT = """你是定时任务解析器。把用户的自然语言转成结构化 JSON。

支持的任务类型:
- periodic_ask: 周期性问 bot 一个问题,收到答复发给用户。params={"question": "..."}

输出 JSON:
{
  "task_type": "periodic_ask",
  "cron_expr": "0 9 * * 1",
  "params": {"question": "本周项目进展如何"},
  "summary": "每周一 09:00 问'本周项目进展如何'"
}

cron_expr 用 5 字段格式: "分 时 日 月 星期"。
- 时间一律按中国时区(CST, UTC+8)解读和书写,运行时调度也按 CST。
  比如用户说「早上 9 点」就写 "0 9 * * *",不要换算 UTC。
- 星期用三字母英文缩写避免歧义: mon, tue, wed, thu, fri, sat, sun。
  范围 "mon-fri" / 列表 "mon,wed,fri" / 不限定 "*" 都可以。
- 日 / 月 / 时 / 分 用数字。不确定的字段填 *

如果用户描述不明确(没说频率 / 没说要问什么),输出 {"error": "需要补充: <什么>"}
只输出 JSON,不要 markdown 代码块。"""


# 数字 dow → 字母,适配标准 cron 习惯(0=Sun) → APScheduler 的字母语义(无歧义)
_DOW_NUM_TO_LETTER = {
    "0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun",
}


def _normalize_dow_token(tok: str) -> str:
    """转一个 dow 子 token: 数字 → 字母;其它原样返(字母 / 字母-字母 / 字母,字母 / *)。"""
    tok = tok.strip().lower()
    if tok in _DOW_NUM_TO_LETTER:
        return _DOW_NUM_TO_LETTER[tok]
    if "-" in tok:
        a, b = tok.split("-", 1)
        return f"{_normalize_dow_token(a)}-{_normalize_dow_token(b)}"
    return tok


def _normalize_dow(field: str) -> str:
    if field == "*":
        return field
    return ",".join(_normalize_dow_token(p) for p in field.split(","))


def build_trigger(cron_expr: str, tz) -> CronTrigger:
    """5 字段 cron → CronTrigger,统一 dow 字母语义。tz 必须传。"""
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 必须 5 字段,得到 {len(parts)}: {cron_expr!r}")
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_normalize_dow(dow),
        timezone=tz,
    )


def _validate_cron(expr: str) -> bool:
    try:
        from datetime import timezone

        build_trigger(expr, timezone.utc)
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_request(text: str) -> tuple[ParsedSchedule | None, str]:
    """返回 (ParsedSchedule, error_msg)。成功 -> (obj, "");失败 -> (None, "原因")。

    error_msg 直接给用户看(中文),不抛异常。
    """
    if not text.strip():
        return None, "请描述要创建的定时任务"
    try:
        reply = run(
            "schedule_parse",
            system=_SYSTEM_PROMPT,
            user=text.strip(),
            temperature=0.0,
            max_tokens=256,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("schedule_parse llm failed: %s", e)
        return None, f"解析失败({type(e).__name__})"

    # 抠 JSON
    s = reply.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        return None, f"解析失败,LLM 输出不是 JSON: {s[:80]}"
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError as e:
        return None, f"解析失败,JSON 格式错: {e}"

    if not isinstance(data, dict):
        return None, "解析失败,根不是 JSON object"

    if "error" in data:
        return None, str(data["error"])

    task_type = str(data.get("task_type", "")).strip()
    cron_expr = str(data.get("cron_expr", "")).strip()
    params = data.get("params") or {}
    summary = str(data.get("summary", "")).strip()

    if task_type != "periodic_ask":
        return None, f"暂不支持的任务类型: {task_type or '(空)'}"
    if not isinstance(params, dict) or not str(params.get("question", "")).strip():
        return None, "periodic_ask 需要 params.question(要问 bot 什么)"
    if not cron_expr or not _validate_cron(cron_expr):
        return None, f"cron 表达式不合法: {cron_expr or '(空)'}"
    if not summary:
        summary = f"{cron_expr} 执行 {task_type}"

    return (
        ParsedSchedule(
            cron_expr=cron_expr,
            task_type=task_type,
            params=params,
            summary=summary,
        ),
        "",
    )
