"""定时任务执行体。MVP 只实现 periodic_ask。

每个 task type 一个 async 函数,接受 snapshot dict(task 字段拷贝),自己负责发消息。
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)


async def _periodic_ask(snapshot: dict) -> None:
    """到点跑 ask runtime 拿答复,把答复发给 receiver。"""
    from helper.ask import ask
    from helper.ask.runtime import render_for_wave
    from helper.im import wave_client
    from helper.im.wave_client import WaveAPIError

    try:
        params = json.loads(snapshot.get("params_json") or "{}")
    except json.JSONDecodeError:
        log.warning("task #%d: bad params_json", snapshot.get("id"))
        return

    question = str(params.get("question", "")).strip()
    if not question:
        log.warning("task #%d periodic_ask missing question", snapshot.get("id"))
        return

    # ask 是同步 SDK 调用,扔到线程池别堵 event loop
    ans = await asyncio.to_thread(
        ask,
        question,
        asker_domain=snapshot.get("owner_user_id", ""),
    )

    body = render_for_wave(ans)
    receiver_id = snapshot.get("receiver_id", "")
    receiver_id_type = snapshot.get("receiver_id_type", "user_id")
    summary = snapshot.get("summary", "")
    text_msg = f"⏰ 定时任务: {summary}\n\n问: {question}\n\n答:\n{body}"

    if not receiver_id:
        log.warning("task #%d no receiver", snapshot.get("id"))
        return

    try:
        await asyncio.to_thread(
            wave_client.send_message,
            receiver_id,
            msg_type="text",
            content={"text": text_msg},
            receiver_id_type=receiver_id_type,
            send_type=1,
        )
    except WaveAPIError as e:
        log.warning("task #%d send_message failed: %s", snapshot.get("id"), e)


_DISPATCHERS = {
    "periodic_ask": _periodic_ask,
}


async def dispatch(snapshot: dict) -> None:
    fn = _DISPATCHERS.get(snapshot.get("task_type", ""))
    if fn is None:
        log.warning("task #%d: unsupported task_type=%s", snapshot.get("id"), snapshot.get("task_type"))
        return
    await fn(snapshot)
