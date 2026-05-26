"""scheduler — 用户对话创建的定时任务。

入口:
- parser.parse_request(text) — LLM 解析自然语言 → ParsedSchedule
- handlers.handle_create / handle_confirm / handle_list / handle_cancel
- runner.start_scheduler / runner.stop_scheduler — FastAPI 启动/关闭挂这俩

任务类型(MVP 只实现 periodic_ask):
- periodic_ask: 到点拿 params.question 跑 ask runtime,把答复发给 receiver
- weekly_report / monthly_report / spec_freshness: 后续迭代
"""

from helper.scheduler.handlers import (
    get_pending_confirm,
    handle_cancel,
    handle_confirm,
    handle_create,
    handle_list,
    is_yes_reply,
)
from helper.scheduler.parser import ParsedSchedule, parse_request
from helper.scheduler.runner import start_scheduler, stop_scheduler

__all__ = [
    "ParsedSchedule",
    "get_pending_confirm",
    "handle_cancel",
    "handle_confirm",
    "handle_create",
    "handle_list",
    "is_yes_reply",
    "parse_request",
    "start_scheduler",
    "stop_scheduler",
]
