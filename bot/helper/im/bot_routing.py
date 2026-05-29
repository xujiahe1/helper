"""bot 路由 — helper 把用户问题转发给外部 bot, 等回执后回贴到原会话。

工作流(配合 ask runtime 的 RouteRequest):
1. 用户群聊 @helper 问"查 app_id xxx 对应的 agent"
2. memory 里写过"查 app_id 类问题 @ tachi 处理, app_id=cli_7847..."
3. ask runtime LLM 看到 memory + 当前问题, 输出 {action: route, target_app_id, via_label, forwarded_text}
4. wave_actions 走到本模块 dispatch_route(): 私聊 tachi 发 rich_text(@tachi + forwarded_text),
   写一行 PendingRouting (含 thinking_tracker 卡片 id)
5. tachi 私聊回复 helper → wave_webhook 在白名单分发前看到 sender.id_type=app_id, 直接调
   handle_bot_reply(); 不走原 raw_inputs 落库链路(避免别 bot 消息污染语料)
6. handle_bot_reply 找最近 5min 未消费的 PendingRouting (target_app_id 匹配),
   按 original_chat_id 决定群里 / 私聊回贴, 同时把 tracker 卡片原地替换为最终答案

超时: scheduler 每分钟扫一次, 5min 没回的 PendingRouting 标 expired,
      给用户发"<via_label> 没回, 你直接 @ 他试试"。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from helper.im import wave_client
from helper.im.progress_card import ThinkingTracker
from helper.im.wave_client import WaveAPIError
from helper.storage import session
from helper.storage.models import PendingRouting, _utcnow

log = logging.getLogger(__name__)


ROUTING_TTL = timedelta(minutes=5)


def _build_at_text_content(target_app_id: str, text: str) -> dict[str, Any]:
    """rich_text body: 第一个 at 节点 + 一段 text。"""
    return {
        "tags": [
            {
                "tag": 0,
                "items": [
                    {"type": "at", "content": {"id": target_app_id, "id_type": "app_id"}},
                    {"type": "text", "content": {"text": " " + text}},
                ],
            }
        ]
    }


def dispatch_route(
    *,
    target_app_id: str,
    via_label: str,
    forwarded_text: str,
    original_raw_id: int,
    original_chat_id: str,
    original_wave_msg_id: str,
    original_asker_domain: str,
    tracker: ThinkingTracker | None,
) -> bool:
    """ask runtime 判定要路由 → 私聊 target bot 转发, 写 PendingRouting。

    返回 True = 私聊发出去 + DB 行落了; False = 私聊失败, 调用方走自己回答兜底。
    tracker 可能是 None(单聊里 ThinkingTracker 不一定建得起来), 容忍。
    """
    if not target_app_id or not forwarded_text:
        return False

    content = _build_at_text_content(target_app_id, forwarded_text)
    try:
        resp = wave_client.send_message(
            target_app_id,
            receiver_id_type="app_id",
            msg_type="rich_text",
            content=content,
        )
    except WaveAPIError as e:
        log.warning("dispatch_route send failed target=%s err=%s", target_app_id, e)
        return False

    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    sent_msg_id = data.get("msg_id", "") if isinstance(data, dict) else ""

    with session() as s:
        s.add(
            PendingRouting(
                sent_msg_id=sent_msg_id,
                target_app_id=target_app_id,
                via_label=via_label or target_app_id,
                original_raw_id=original_raw_id,
                original_chat_id=original_chat_id,
                original_wave_msg_id=original_wave_msg_id,
                original_asker_domain=original_asker_domain,
                tracker_card_msg_id=(tracker.msg_id if tracker else ""),
                tracker_receiver_id=(tracker.receiver_id if tracker else ""),
                tracker_receiver_id_type=(tracker.receiver_id_type if tracker else ""),
            )
        )
    return True


def _extract_reply_text(payload: dict[str, Any]) -> str:
    """从 webhook 入站 payload 抽出对方 bot 回复的纯文本。

    支持 text / rich_text / card(card 里 i18n_text.zh-cn 或 markdown 节点)。
    抽不到返空串, 上层降级文案"<via_label> 回复了, 但内容拿不到, 你去会话看一下"。
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return ""
    msg = event.get("message")
    if not isinstance(msg, dict):
        return ""
    content_str = msg.get("content")
    if not isinstance(content_str, str):
        return ""
    try:
        inner = json.loads(content_str)
    except json.JSONDecodeError:
        return ""
    if not isinstance(inner, dict):
        return ""

    # text
    t = inner.get("text") or inner.get("content")
    if isinstance(t, str) and t.strip():
        return t.strip()

    # rich_text
    tags = inner.get("tags")
    if isinstance(tags, list):
        chunks: list[str] = []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            for it in tag.get("items", []) or []:
                if not isinstance(it, dict):
                    continue
                c = it.get("content")
                if not isinstance(c, dict):
                    continue
                tt = it.get("type")
                if tt == "text" and isinstance(c.get("text"), str):
                    chunks.append(c["text"])
                elif tt == "url" and isinstance(c.get("url"), str):
                    chunks.append(c["url"])
            chunks.append("\n")
        joined = "".join(chunks).strip()
        if joined:
            return joined

    # card with i18n_text(我们见过 tachi 回的就是这种)
    i18n = inner.get("i18n_text")
    if isinstance(i18n, dict):
        for key in ("zh-cn", "zh", "en"):
            v = i18n.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # card.elements[].text(markdown 节点)
    card = inner.get("card")
    if isinstance(card, dict):
        elems = card.get("elements")
        if isinstance(elems, list):
            chunks = []
            for e in elems:
                if isinstance(e, dict) and isinstance(e.get("text"), str):
                    chunks.append(e["text"])
            joined = "\n".join(chunks).strip()
            if joined:
                return joined

    return ""


def _format_final_reply(answer_text: str, via_label: str, asker_domain: str = "") -> str:
    """最终给用户看的文本: 答案 + 归属。

    群聊里 asker_domain 非空时, 在最前面 @他;
    私聊不需要 @, asker_domain 可空。
    归属"—— 答复来自 @<via_label>"放尾(已和用户对齐)。
    """
    parts: list[str] = []
    if asker_domain:
        parts.append(f"@{asker_domain}")
    parts.append(answer_text or "(空回复)")
    parts.append(f"\n—— 答复来自 @{via_label}")
    return "\n".join(parts)


def handle_bot_reply(payload: dict[str, Any], *, sender_app_id: str) -> bool:
    """target bot 私聊回 helper → 找最近未消费 PendingRouting → 回贴 + 标 consumed。

    返回 True = 关联到一条 routing 并回贴; False = 没找到 routing(应丢弃, 不走 raw_inputs)。
    """
    if not sender_app_id:
        return False

    answer_text = _extract_reply_text(payload)
    cutoff = _utcnow() - ROUTING_TTL

    with session() as s:
        row = s.execute(
            select(PendingRouting)
            .where(PendingRouting.target_app_id == sender_app_id)
            .where(PendingRouting.consumed_at.is_(None))
            .where(PendingRouting.expired_at.is_(None))
            .where(PendingRouting.created_at >= cutoff)
            .order_by(PendingRouting.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            log.info(
                "bot reply with no pending routing sender=%s, drop", sender_app_id
            )
            return False
        routing_id = row.id
        chat_id = row.original_chat_id
        asker = row.original_asker_domain
        via = row.via_label
        tracker_card = row.tracker_card_msg_id
        wave_quote = row.original_wave_msg_id

    final_text = _format_final_reply(
        answer_text or "(对方回了, 但内容抽不到, 请直接看会话)",
        via_label=via,
        asker_domain=asker if chat_id else "",  # 私聊不必 @
    )

    delivered = False
    # 1) 优先原地替换 thinking 卡片(用户感知一气呵成)
    if tracker_card:
        try:
            wave_client.update_card_active(
                tracker_card,
                content={
                    "card": {
                        "tag": "flow",
                        "elements": [
                            {"tag": "markdown", "text": final_text, "text_align": "left"},
                        ],
                    }
                },
            )
            delivered = True
        except WaveAPIError as e:
            log.warning("update_card_active failed card=%s: %s", tracker_card, e)

    # 2) 卡片更新失败 / 没 tracker → 发新消息(群里 reply 用户原问题, 私聊直接发 sender)
    if not delivered:
        try:
            if chat_id:
                if wave_quote:
                    wave_client.reply_message(
                        wave_quote, msg_type="text", content={"text": final_text}
                    )
                else:
                    wave_client.send_message(
                        chat_id,
                        receiver_id_type="chat_id",
                        msg_type="text",
                        content={"text": final_text},
                    )
            elif asker:
                wave_client.send_message(
                    asker,
                    receiver_id_type="user_id",
                    msg_type="text",
                    content={"text": final_text},
                )
            delivered = True
        except WaveAPIError as e:
            log.warning("bot reply fallback send failed: %s", e)

    with session() as s:
        r = s.get(PendingRouting, routing_id)
        if r is not None and r.consumed_at is None:
            r.consumed_at = _utcnow()
    return True


def expire_old_routings() -> int:
    """5min 没回的 PendingRouting 标 expired, 给用户发"<via_label> 没回, 你直接 @ 他试试"。

    scheduler 每分钟跑一次。返回这次过期了几条。
    """
    cutoff = _utcnow() - ROUTING_TTL
    with session() as s:
        rows = s.execute(
            select(PendingRouting)
            .where(PendingRouting.consumed_at.is_(None))
            .where(PendingRouting.expired_at.is_(None))
            .where(PendingRouting.created_at < cutoff)
        ).scalars().all()
        rid_pack = [
            (
                r.id, r.via_label, r.original_chat_id,
                r.original_wave_msg_id, r.original_asker_domain,
                r.tracker_card_msg_id,
            )
            for r in rows
        ]

    expired = 0
    for rid, via, chat_id, wave_quote, asker, tracker_card in rid_pack:
        timeout_text = (
            f"⏰ @{via} 5 分钟内没回, 你可以直接 @ 它再问一次"
        )
        target_addressed = f"@{asker}\n{timeout_text}" if (asker and chat_id) else timeout_text

        delivered = False
        if tracker_card:
            try:
                wave_client.update_card_active(
                    tracker_card,
                    content={
                        "card": {
                            "tag": "flow",
                            "elements": [
                                {"tag": "markdown", "text": target_addressed, "text_align": "left"},
                            ],
                        }
                    },
                )
                delivered = True
            except WaveAPIError as e:
                log.warning("expire update_card failed card=%s: %s", tracker_card, e)

        if not delivered:
            try:
                if chat_id:
                    if wave_quote:
                        wave_client.reply_message(
                            wave_quote, msg_type="text", content={"text": target_addressed}
                        )
                    else:
                        wave_client.send_message(
                            chat_id, receiver_id_type="chat_id",
                            msg_type="text", content={"text": target_addressed},
                        )
                elif asker:
                    wave_client.send_message(
                        asker, receiver_id_type="user_id",
                        msg_type="text", content={"text": target_addressed},
                    )
            except WaveAPIError as e:
                log.warning("expire fallback send failed routing=%d: %s", rid, e)

        with session() as s:
            r = s.get(PendingRouting, rid)
            if r is not None and r.expired_at is None and r.consumed_at is None:
                r.expired_at = _utcnow()
                expired += 1

    return expired
