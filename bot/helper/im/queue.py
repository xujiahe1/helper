"""后台异步任务的统一池子 — 解决两个问题:

1) loop.create_task() 不会保引用,GC 可能在 task 完成前回收,导致悄无声息丢任务
   → 维护一个模块级 set 强引用,task 完成时自摘
2) 高并发下 ask/L1/intent_classify 等 LLM 调用并行打满,Athenai 触发限流(retcode=10101016)
   → 给 LLM 调用加 Semaphore,默认 5 路并发上限

用法:
    from helper.im.queue import spawn, llm_slot

    spawn(some_coro())             # 替代 loop.create_task

    async with llm_slot():
        await asyncio.to_thread(llm_call)

shutdown:
    await drain(timeout=5)         # FastAPI shutdown 钩子调,等 5 秒后强行退出
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

_pending: set[asyncio.Task] = set()

# 5 路并发对单进程 / 单 bot 的负载够用。Athenai 文档没明示 RPM 上限,
# 5 路 + 3-8s/次 = ~50-100 RPM,远低于一般阈值。需要时调高这个常数。
_DEFAULT_LLM_CONCURRENCY = 5
_llm_sem: asyncio.Semaphore | None = None


def _get_llm_sem() -> asyncio.Semaphore:
    """惰性建 — Semaphore 必须绑定一个 event loop。在 webhook 路径里第一次用时建。"""
    global _llm_sem
    if _llm_sem is None:
        _llm_sem = asyncio.Semaphore(_DEFAULT_LLM_CONCURRENCY)
    return _llm_sem


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    """fire-and-forget 但保留引用 + 异常 log。

    在没有 running loop 的同步上下文(如测试 / CLI)调,直接 asyncio.run 不合适
    (会嵌套 loop),退回到 None — 调用方自行处理 fallback(走 sync 路径)。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    task.add_done_callback(_log_if_failed)
    return task


def _log_if_failed(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception("background task failed", exc_info=exc)


@asynccontextmanager
async def llm_slot():
    """LLM 调用前包一层。block 直到拿到 slot。"""
    sem = _get_llm_sem()
    async with sem:
        yield


async def drain(timeout: float = 5.0) -> int:
    """等所有 pending task 完成或超时。返回剩余未完成数量。"""
    if not _pending:
        return 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*_pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        pass
    return len(_pending)


def pending_count() -> int:
    return len(_pending)
