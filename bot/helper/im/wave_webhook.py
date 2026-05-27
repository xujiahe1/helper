"""Wave IM 回调端点 — AES 解密 + 签名校验 + event_id 去重 + 1s 响应。

协议参考 KM mheo000ok1zs(事件订阅概述)/ mh4sbu0higfc(回调地址校验事件)
/ mhmjqlrjlehq(接收消息 v2)。要点:
  - Header: hoyowave-open-signature / -timestamp / -nonce / -appid
  - 加密: AES-CBC, key=secretKey.encode('utf-8'), iv=key[:16], PKCS7,
    密文 base64,Body 形如 {"encrypt": "<base64>"}。
    AES 变种由 key 字节数决定(16/24/32 → AES-128/192/256)。
  - 签名: sha256(timestamp + nonce + RAW_BODY_STRING + sign_token),
    其中 raw body 指**收到的整个 http body 字符串**(还未解密),hex digest 比对。
  - 校验事件(URL 配置时下发, event_type=open.callbackurl.updated_v1):
    challenge 嵌在 **event.challenge**(不是顶层),1.5s 内回
    {"challenge": "<原值>"}。
  - 普通事件: 1s 内 200 + ""(或 "{}"),event_id 7.1h 内去重,落 raw_inputs。

事件 payload 抽字段(v2 协议):
  - event.message.{msg_id, msg_type, content, mentions[], thread_id, quote_msg_id, recalled}
  - event.sender.{id, id_type, user_id, tenant_id}        ← v2 扁平,**不**嵌套 sender_id
  - event.receiver.{id, id_type}                          ← chat_id=群 / app_id=单聊到 bot
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from base64 import b64decode
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, Header, HTTPException, Request, Response

from helper.config import get_settings
from helper.im import feedback as fb
from helper.im.wave_actions import schedule_ask_reply, schedule_post_message
from helper.ingest import schedule_l1
from helper.storage import raw_store, session
from helper.storage.models import WaveEventDedup

log = logging.getLogger(__name__)

router = APIRouter()


# ---------- 加解密 / 验签 ----------

def _decrypt(encrypted_b64: str, secret_key: str) -> bytes:
    """AES-CBC 解密。key/iv 规则按 KM 文档 4.2.3。"""
    key = secret_key.encode("utf-8")
    iv = key[:16]
    ct = b64decode(encrypted_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    # PKCS7 trim
    pad = pt[-1]
    if pad < 1 or pad > 16 or pt[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad PKCS7 padding")
    return pt[:-pad]


def _verify_signature(
    timestamp: str, nonce: str, raw_body: bytes, sign_token: str, expected: str
) -> bool:
    msg = timestamp.encode("utf-8") + nonce.encode("utf-8") + raw_body + sign_token.encode("utf-8")
    actual = hashlib.sha256(msg).hexdigest()
    return hmac.compare_digest(actual, expected)


# ---------- payload 抽字段(v2 协议) ----------

def _extract_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """返回 event.message 字典,不存在返 None。"""
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    msg = event.get("message")
    return msg if isinstance(msg, dict) else None


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    """从 Wave 消息事件里抠出用户的纯文本。

    Wave/Lark 协议: event.message.content 是个 JSON 字符串,text 类消息形如
    {"text": "实际内容"};rich_text 形如 {"tags":[{"items":[{"type":"text","content":{"text":"..."}}, ...]}]};
    其它类型(image / video / file / card)就没有可读文本。抽不到返 None,调用方退化存原文。
    """
    msg = _extract_message(payload)
    if msg is None:
        return None
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    try:
        inner = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(inner, dict):
        return None
    # text 类
    text = inner.get("text") or inner.get("content")
    if isinstance(text, str) and text.strip():
        return text.strip()
    # rich_text 类: 把所有段的 text 拼起来
    tags = inner.get("tags")
    if isinstance(tags, list):
        chunks: list[str] = []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            items = tag.get("items")
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("type") == "text":
                    c = it.get("content")
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        chunks.append(c["text"])
            chunks.append("\n")
        joined = "".join(chunks).strip()
        if joined:
            return joined
    return None


def _extract_sender(payload: dict[str, Any]) -> tuple[str, str]:
    """返回 (sender_id, id_type)。v2 协议 sender 是扁平结构。

    payload.event.sender = {id, id_type, user_id?, tenant_id} —— 注意 v2 中:
      - id: 通常是 union_id (ou_xxx),id_type 字段标了具体类型(union_id / app_id)
      - user_id: 同时给到的域账号 (jiahe.xu),如果应用对该用户有 contact:user 权限就有
      - id_type=app_id 表示发送者是机器人(包括别的机器人 @ 我们);跳过这种发送者

    优先级: 直接用 user_id(域账号)→ id_type=user_id 返回;
            没 user_id 用 id(union_id)→ id_type=union_id;
            id_type=app_id 视为非用户来源,返空串。
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return "", ""
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return "", ""
    id_type = sender.get("id_type")
    if id_type == "app_id":  # 机器人发的,不是用户
        return "", ""
    user_id = sender.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id, "user_id"
    sid = sender.get("id")
    if isinstance(sid, str) and sid:
        return sid, "union_id" if id_type == "union_id" else (id_type or "")
    return "", ""


def _extract_chat_id(payload: dict[str, Any]) -> str:
    """群聊: receiver.id_type=chat_id → 返 chat_id;单聊到 bot 返空串。"""
    event = payload.get("event")
    if not isinstance(event, dict):
        return ""
    receiver = event.get("receiver")
    if not isinstance(receiver, dict):
        return ""
    if receiver.get("id_type") == "chat_id":
        rid = receiver.get("id")
        return rid if isinstance(rid, str) else ""
    return ""


def _is_at_bot(payload: dict[str, Any], our_app_id: str) -> bool:
    """判断这条消息是否真的 @ 了本 bot。

    判定规则:
      - 单聊(receiver.id_type=app_id 且 id 是本 bot)→ 视为 True
      - 群聊看 mentions[]: 任一项 id_type=app_id 且 id=本 bot → True
                         任一项 id_type=all → True(全员被算上)
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return False

    # 单聊
    receiver = event.get("receiver")
    if isinstance(receiver, dict):
        if receiver.get("id_type") == "app_id" and receiver.get("id") == our_app_id:
            return True

    # 群聊 mentions
    msg = event.get("message")
    if isinstance(msg, dict):
        mentions = msg.get("mentions")
        if isinstance(mentions, list):
            for m in mentions:
                if not isinstance(m, dict):
                    continue
                if m.get("id_type") == "all":
                    return True
                if m.get("id_type") == "app_id" and m.get("id") == our_app_id:
                    return True
    return False


def _seen_event(event_id: str) -> bool:
    """7.1h 窗口去重。命中返 True;未命中则插入并返 False。

    KM 文档说重发窗口 ~7.1h,这里不做 TTL GC,每条 event_id 永久占位。
    sqlite 单表存 7.1h * 平均 QPS 的量级,M1 完全够用。后续如要 GC
    走单独清理 job,不是热路径关心的事。
    """
    with session() as s:
        if s.get(WaveEventDedup, event_id) is not None:
            return True
        s.add(WaveEventDedup(event_id=event_id))
    return False


# ---------- 路由 ----------

@router.post("/callback")
async def wave_callback(
    request: Request,
    sig: str | None = Header(default=None, alias="hoyowave-open-signature"),
    ts: str | None = Header(default=None, alias="hoyowave-open-timestamp"),
    nonce: str | None = Header(default=None, alias="hoyowave-open-nonce"),
) -> Response:
    s = get_settings()
    if not s.wave_callback_configured:
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info(
            "WAVE_DEBUG entry client=%s headers=%s wave_callback_configured=False",
            request.client.host if request.client else None,
            dict(request.headers),
        )
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=503 body=%r", "wave callback not configured")
        # 未配置就不该收事件,直接 503 让对方重试不到
        raise HTTPException(status_code=503, detail="wave callback not configured")

    raw_body = await request.body()

    # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
    from base64 import b64encode as _b64encode
    log.info(
        "WAVE_DEBUG entry client=%s headers=%s raw_body_repr=%r raw_body_b64=%s",
        request.client.host if request.client else None,
        dict(request.headers),
        raw_body,
        _b64encode(raw_body).decode("ascii"),
    )

    # 1) 验签 — 缺 header 一律拒,签错也拒
    if not (sig and ts and nonce):
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=401 body=%r", "missing signature headers")
        raise HTTPException(status_code=401, detail="missing signature headers")
    if not _verify_signature(ts, nonce, raw_body, s.wave_callback_sign_token, sig):
        log.warning("wave webhook: bad signature ts=%s nonce=%s", ts, nonce)
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=401 body=%r", "bad signature")
        raise HTTPException(status_code=401, detail="bad signature")

    # 2) 解外层 envelope { "encrypt": "<base64>" }
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=400 body=%r", "body not JSON")
        raise HTTPException(status_code=400, detail="body not JSON") from None
    encrypted = envelope.get("encrypt") if isinstance(envelope, dict) else None
    if not isinstance(encrypted, str):
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=400 body=%r", "missing encrypt field")
        raise HTTPException(status_code=400, detail="missing encrypt field")

    # 3) 解密 → 明文 JSON
    try:
        plaintext = _decrypt(encrypted, s.wave_callback_aes_key).decode("utf-8")
        payload: dict[str, Any] = json.loads(plaintext)
    except Exception as e:  # noqa: BLE001
        log.exception("wave webhook: decrypt failed")
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=400 body=%r", f"decrypt failed: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"decrypt failed: {type(e).__name__}") from e

    # 4) 校验事件(URL 配置时下发):明文 event.challenge 存在就回显
    #    协议参考 KM mh4sbu0higfc(回调地址校验事件)。事件类型 open.callbackurl.updated_v1,
    #    challenge 嵌在 event.challenge 而不是顶层。1.5s 内回 {"challenge": "<原值>"}。
    event_obj = payload.get("event")
    challenge = event_obj.get("challenge") if isinstance(event_obj, dict) else None
    # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
    _header_for_debug = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    log.info(
        "WAVE_DEBUG challenge_parse event_type=%r challenge=%r event_obj=%r",
        _header_for_debug.get("event_type") if isinstance(_header_for_debug, dict) else None,
        challenge,
        event_obj,
    )
    if isinstance(challenge, str) and challenge:
        _challenge_body = json.dumps({"challenge": challenge})
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=200 body=%s", _challenge_body)
        return Response(
            content=_challenge_body,
            media_type="application/json",
        )

    # 5) 普通事件 — header.event_id 7.1h 去重(sqlite write,丢线程池避免阻塞 event loop)
    header = payload.get("header") or {}
    event_id = header.get("event_id") if isinstance(header, dict) else None
    if isinstance(event_id, str) and event_id:
        seen = await asyncio.to_thread(_seen_event, event_id)
        if seen:
            log.info("wave webhook: duplicate event_id=%s, skip", event_id)
            # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
            log.info("WAVE_DEBUG response status=200 body=%r reason=duplicate", "")
            return Response(content="", media_type="application/json")
    else:
        log.warning("wave webhook: event without event_id, payload keys=%s", list(payload.keys()))

    event_type = header.get("event_type", "") if isinstance(header, dict) else ""

    # 5.5) feedback 事件(👍/👎)单独路由,不落 raw_inputs
    if fb.is_feedback_event(event_type):
        try:
            fb.handle(payload)
        except Exception:  # noqa: BLE001
            log.exception("feedback handle failed event_id=%s", event_id)
        # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
        log.info("WAVE_DEBUG response status=200 body=%r reason=feedback", "")
        return Response(content="", media_type="application/json")

    # 6) 落 raw_inputs
    #    - content_text: 优先抽消息纯文本(L1 直接消费);抽不到退化到完整明文 JSON
    #    - attachments_json: 永远存完整 envelope,留作审计 / 后续重抽
    #    - author_domain: 先落 sender_id 占位,后台 schedule_post_message 反查到域账号再回填
    extracted = _extract_message_text(payload)
    content_text = extracted if extracted is not None else plaintext
    sender_id, sender_id_type = _extract_sender(payload)
    chat_id = _extract_chat_id(payload)
    is_at_bot = _is_at_bot(payload, s.wave_app_id)
    msg = _extract_message(payload) or {}
    media_type = msg.get("msg_type", "") if isinstance(msg.get("msg_type"), str) else ""
    wave_msg_id = msg.get("msg_id", "") if isinstance(msg.get("msg_id"), str) else ""
    parent_msg_id = msg.get("quote_msg_id", "") if isinstance(msg.get("quote_msg_id"), str) else ""
    thread_id = msg.get("thread_id", "") if isinstance(msg.get("thread_id"), str) else ""

    def _persist_raw() -> int:
        with session() as sess:
            row = raw_store.append(
                sess,
                source_type=f"im_wave:{event_type}" if event_type else "im_wave",
                source_ref=event_id or "",
                content_text=content_text,
                # 已经是 user_id(域账号)就直接落,否则占位 sender_id 等后台回填
                author_domain=sender_id if sender_id_type == "user_id" else sender_id,
                attachments_json=plaintext,
                chat_id=chat_id,
                is_at_bot=is_at_bot,
                parent_message_id=parent_msg_id,
                thread_id=thread_id,
                media_type=media_type,
                wave_message_id=wave_msg_id,
                # forward_from_* 协议没原生字段,留空,后续 LLM 抽
            )
            return row.id

    raw_id = await asyncio.to_thread(_persist_raw)

    # 7) 后台 fire-and-forget(都塞不进 1s 回调窗口):
    #    - 群消息但没 @bot → 只落 raw,不调 LLM(成本控制,见 docs/runtime.md §2.3)
    #    - 单聊或群里 @bot → 跑 intent classify,ask 路径走 ask runtime + 引用回复;
    #                       judgment 路径走 L1 + ack
    is_message_event = extracted is not None
    is_active_target = is_at_bot or not chat_id  # 单聊 chat_id 空也视为主动目标
    if is_message_event and is_active_target:
        schedule_ask_reply(
            raw_id=raw_id,
            text=extracted or "",
            sender_id=sender_id,
            sender_id_type=sender_id_type,
            chat_id=chat_id,
            wave_msg_id=wave_msg_id,
        )
    else:
        # 群里"听"消息:预筛后才跑 L1(关键词命中走完整 L1,边缘情况 mini 兜底);反查身份不发回复
        schedule_l1(raw_id, prefilter=True)
        schedule_post_message(
            raw_id, sender_id, sender_id_type, send_ack=False
        )

    # 8) 1s 内 200 + 空 body
    # WAVE_DEBUG: 临时调试,拿到完整请求后立刻删
    log.info("WAVE_DEBUG response status=200 body=%r reason=normal_event", "")
    return Response(content="", media_type="application/json")
