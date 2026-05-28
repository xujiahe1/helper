"""ThinkingTracker — 长路径"思考中"卡片 + 主动更新成最终结果。

bot 处理一条消息可能要 5-15s(KM fetch + L1 抽 + LLM 推理),
用户视角看是「发出去 → 长时间沉默 → 一坨结果」,体验糟糕。
方案: 收到消息立刻发一张极简卡片"思考中",处理完成后用
card/active/update 原地刷新成最终内容。

设计原则:
- 状态机只有 thinking → done(成功)/ done(失败),不展示阶段细节
- 失败统一文案"⚠️ 出问题了,稍后重试",不暴露技术细节
- start 失败不抛(不能因为卡片发不出来就让主路径退出);
  finish/fail 也只 warn,失败兜底用 send_message 重发文本

调用约定:
    tracker = ThinkingTracker(receiver_id="chat_id_or_user_id", receiver_id_type="chat_id").start()
    try:
        body = ...do work...
        tracker.finish(body)
    except Exception:
        tracker.fail()
"""

from __future__ import annotations

import logging
from typing import Any

from helper.im import wave_client
from helper.im.wave_client import WaveAPIError

log = logging.getLogger(__name__)

THINKING_TEXT = "🤔 思考中..."
ERROR_TEXT = "⚠️ 出问题了,稍后重试"


def _card_payload(text: str) -> dict[str, Any]:
    """最简卡片 schema — header 留空,正文一个 markdown 组件。

    flow tag = 流式布局容器(从主动更新文档示例里抠出来的最简结构)。
    用 markdown 是为了 finish 时能把 ask 答案的换行 / 引用块原样渲染。
    """
    return {
        "header": {"title": ""},
        "card": {
            "tag": "flow",
            "elements": [
                {"tag": "markdown", "text": text, "text_align": "left"},
            ],
        },
    }


class ThinkingTracker:
    """单条消息处理生命周期内的卡片管理器。"""

    def __init__(
        self,
        *,
        receiver_id: str,
        receiver_id_type: str,
        request_id_prefix: str = "thinking",
    ) -> None:
        self.receiver_id = receiver_id
        self.receiver_id_type = receiver_id_type
        self._request_id_prefix = request_id_prefix
        self.msg_id: str = ""
        self._finished: bool = False

    def start(self) -> "ThinkingTracker":
        """发"思考中"卡片;失败只 log,不抛。"""
        if not self.receiver_id or self.receiver_id_type not in ("user_id", "union_id", "chat_id"):
            log.info(
                "thinking-card start skipped: id_type=%s not supported",
                self.receiver_id_type,
            )
            return self
        try:
            resp = wave_client.send_message(
                self.receiver_id,
                msg_type="card",
                content=_card_payload(THINKING_TEXT),
                receiver_id_type=self.receiver_id_type,
                send_type=1,
                request_id=f"{self._request_id_prefix}-start",
            )
        except WaveAPIError as e:
            log.warning("thinking-card start failed: %s", e)
            return self
        # 兼容 _post 既可能返回 data 也可能返回外层 envelope 的情况
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        msg_id = data.get("msg_id") or data.get("msg_id", "")
        if isinstance(msg_id, str):
            self.msg_id = msg_id
        return self

    def _update(self, text: str) -> bool:
        """原地刷新卡片内容。失败 → False,调用方决定降级。"""
        if not self.msg_id:
            return False
        try:
            wave_client.update_card_active(
                self.msg_id,
                content=_card_payload(text),
            )
            return True
        except WaveAPIError as e:
            log.warning("thinking-card update failed msg=%s: %s", self.msg_id, e)
            return False

    def _fallback_send_text(self, text: str) -> None:
        """卡片刷新失败的兜底:直接发一条文本消息,保证用户看得到结果。"""
        if not self.receiver_id or self.receiver_id_type not in (
            "user_id", "union_id", "chat_id",
        ):
            return
        try:
            wave_client.send_message(
                self.receiver_id,
                msg_type="text",
                content={"text": text},
                receiver_id_type=self.receiver_id_type,
                send_type=1,
            )
        except WaveAPIError as e:
            log.warning("thinking-card fallback send failed: %s", e)

    def finish(self, text: str) -> None:
        if self._finished:
            return
        self._finished = True
        if not text:
            return
        if not self._update(text):
            self._fallback_send_text(text)

    def fail(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self._update(ERROR_TEXT):
            self._fallback_send_text(ERROR_TEXT)
