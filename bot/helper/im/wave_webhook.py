"""Wave IM 回调端点 — AES 解密 + 签名校验 + event_id 去重 + 1s 响应。

协议参考 KM mheo000ok1zs(事件订阅概述)/ mh4sbu0higfc(回调地址校验事件)
/ mhmjqlrjlehq(接收消息 v2)。要点:
  - Header: hoyowave-open-signature / -timestamp / -nonce / -appid
  - 加密: AES-CBC, key=secretKey.encode('utf-8'), iv=key[:16], PKCS7,
    密文 base64,Body 形如 {"encrypt": "<base64>"}。
    AES 变种由 key 字节数决定(16/24/32 → AES-128/192/256)。
  - 签名: sha256(timestamp + nonce + plaintext_body + sign_token)。
    **服务端"先签后加密"**(KM mheo000ok1zs §4.2 原文: "先对原事件数据进行加签,
    再对 Http body 进行加密"),接收方必须**先解密、再用明文 JSON 字符串验签**;
    直接拿 envelope 密文验签会全 401。
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
from helper.im import reaction as rx
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
    timestamp: str, nonce: str, plaintext_body: bytes, sign_token: str, expected: str
) -> bool:
    msg = (
        timestamp.encode("utf-8")
        + nonce.encode("utf-8")
        + plaintext_body
        + sign_token.encode("utf-8")
    )
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


def extract_text_from_content(msg_type: str, content_str: str) -> str | None:
    """从 Wave 消息的 (msg_type, content) 抽纯文本。

    支持:
      - text:        {"text": "..."}                          → 直接返
      - rich_text:   {"tags":[{"items":[{"type":"text"|"url",...}]}]} → 拼接段
      - merge_forward: {"message_list":[{sender, message}, ...]}      → 递归每条嵌套 message
                       格式化成 "[姓名/域账号] 正文" 多行, 单条上限 800 字, 总上限 4000 字
      - card / 其它: 没可读文本, 返 None

    Wave webhook 入站、merge_forward 子消息、OpenAPI message/get 三处都共用这个抽取器。
    """
    if not isinstance(content_str, str):
        return None
    try:
        inner = json.loads(content_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(inner, dict):
        return None

    # text 类
    if msg_type in ("text", ""):
        text = inner.get("text") or inner.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()

    # rich_text 类: 把所有段的 text / url 拼起来。
    # type=="url" 段必须取 content.url 拼进去, 否则用户在消息里粘贴的 KM 链接、
    # 外部链接会被整段丢掉, 下游 km_ingest.find_km_urls 永远找不到 URL。
    if msg_type in ("rich_text", ""):
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
                    if not isinstance(it, dict):
                        continue
                    c = it.get("content")
                    if not isinstance(c, dict):
                        continue
                    t = it.get("type")
                    if t == "text" and isinstance(c.get("text"), str):
                        chunks.append(c["text"])
                    elif t == "url" and isinstance(c.get("url"), str):
                        chunks.append(c["url"])
                chunks.append("\n")
            joined = "".join(chunks).strip()
            if joined:
                return joined

    # merge_forward: 把每条嵌套消息抽成可读文本, 格式 "[发送人] 正文"。
    # bot 自己的回复(card / text 都跳过), 防注入大量 ack 噪音。
    if msg_type == "merge_forward":
        msg_list = inner.get("message_list")
        if isinstance(msg_list, list):
            lines: list[str] = []
            total = 0
            MAX_PER_MSG = 800
            MAX_TOTAL = 4000
            for entry in msg_list:
                if not isinstance(entry, dict):
                    continue
                sender = entry.get("sender") or {}
                inner_msg = entry.get("message") or {}
                if not isinstance(inner_msg, dict):
                    continue
                inner_type = inner_msg.get("msg_type", "") or ""
                inner_content = inner_msg.get("content", "") or ""
                # bot 自己发的卡片 / 文本(/inbox 等) 跳过, 不算原对话内容
                if isinstance(sender, dict) and sender.get("id_type") == "app_id":
                    continue
                inner_text = extract_text_from_content(inner_type, inner_content)
                if not inner_text:
                    continue
                if len(inner_text) > MAX_PER_MSG:
                    inner_text = inner_text[:MAX_PER_MSG] + "…"
                # 发送人 label: 优先 user_id (域账号), 其次 union_id
                who = ""
                if isinstance(sender, dict):
                    if sender.get("id_type") == "user_id":
                        who = sender.get("id") or ""
                    else:
                        who = sender.get("id") or ""
                line = f"[{who}] {inner_text}" if who else inner_text
                lines.append(line)
                total += len(line)
                if total > MAX_TOTAL:
                    lines.append("(以下内容过长已截断)")
                    break
            joined = "\n".join(lines).strip()
            if joined:
                return joined

    return None


def _extract_message_text(payload: dict[str, Any]) -> str | None:
    """从 Wave webhook payload 里抠出用户的纯文本。 实质委托 extract_text_from_content。"""
    msg = _extract_message(payload)
    if msg is None:
        return None
    content = msg.get("content")
    msg_type = msg.get("msg_type", "") if isinstance(msg.get("msg_type"), str) else ""
    if not isinstance(content, str):
        return None
    return extract_text_from_content(msg_type, content)


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
        raise HTTPException(status_code=503, detail="wave callback not configured")

    raw_body = await request.body()

    if not (sig and ts and nonce):
        raise HTTPException(status_code=401, detail="missing signature headers")

    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="body not JSON") from None
    encrypted = envelope.get("encrypt") if isinstance(envelope, dict) else None
    if not isinstance(encrypted, str):
        raise HTTPException(status_code=400, detail="missing encrypt field")

    try:
        plaintext = _decrypt(encrypted, s.wave_callback_aes_key).decode("utf-8")
        payload: dict[str, Any] = json.loads(plaintext)
    except Exception as e:  # noqa: BLE001
        log.exception("wave webhook: decrypt failed")
        raise HTTPException(status_code=400, detail=f"decrypt failed: {type(e).__name__}") from e

    if not _verify_signature(
        ts, nonce, plaintext.encode("utf-8"), s.wave_callback_sign_token, sig
    ):
        log.warning("wave webhook: bad signature ts=%s nonce=%s", ts, nonce)
        raise HTTPException(status_code=401, detail="bad signature")

    event_obj = payload.get("event")
    challenge = event_obj.get("challenge") if isinstance(event_obj, dict) else None
    if isinstance(challenge, str) and challenge:
        return Response(
            content=json.dumps({"challenge": challenge}),
            media_type="application/json",
        )

    header = payload.get("header") or {}
    event_id = header.get("event_id") if isinstance(header, dict) else None
    if isinstance(event_id, str) and event_id:
        seen = await asyncio.to_thread(_seen_event, event_id)
        if seen:
            log.info("wave webhook: duplicate event_id=%s, skip", event_id)
            return Response(content="", media_type="application/json")
    else:
        log.warning("wave webhook: event without event_id, payload keys=%s", list(payload.keys()))

    event_type = header.get("event_type", "") if isinstance(header, dict) else ""

    # 6) 显式事件白名单分发。**未列入白名单的事件一律 drop,不落 raw_inputs**,
    #    避免 reaction / bot.entered / chat.members.* / auth.* / docs.* 等非消息事件
    #    被当成对话语料污染知识管线。
    if fb.is_feedback_event(event_type):
        try:
            fb.handle(payload)
        except Exception:  # noqa: BLE001
            log.exception("feedback handle failed event_id=%s", event_id)
        return Response(content="", media_type="application/json")

    if rx.is_reaction_event(event_type):
        try:
            rx.handle(event_type, payload)
        except Exception:  # noqa: BLE001
            log.exception("reaction handle failed event_id=%s", event_id)
        return Response(content="", media_type="application/json")

    # bot-to-bot 私聊路由回执: sender.id_type=app_id 且不是自己 → 走 bot_routing,
    # 关联回 PendingRouting 把答案回贴到原会话; **不** 落 raw_inputs(防止外 bot 消息污染语料)。
    sender_obj = (payload.get("event") or {}).get("sender") if isinstance(payload.get("event"), dict) else None
    if (
        event_type in {"im.msg.direct.sent_v2", "im.msg.group.sent_v2"}
        and isinstance(sender_obj, dict)
        and sender_obj.get("id_type") == "app_id"
        and sender_obj.get("id") != s.wave_app_id
    ):
        from helper.im.bot_routing import handle_bot_reply
        try:
            handle_bot_reply(payload, sender_app_id=str(sender_obj.get("id") or ""))
        except Exception:  # noqa: BLE001
            log.exception("bot routing reply failed event_id=%s", event_id)
        return Response(content="", media_type="application/json")

    # 只有真正的"接收消息"事件继续走落 raw + LLM 路径
    if event_type not in {"im.msg.direct.sent_v2", "im.msg.group.sent_v2"}:
        log.info("wave webhook: drop non-message event type=%s event_id=%s", event_type, event_id)
        return Response(content="", media_type="application/json")

    # 7) 落 raw_inputs
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

    # 诊断: 入站消息事件统一打一行 WARN, 看 msg_type / quote / extracted 长度 /
    # content 头 200 字。 用于排查 quote 反查不到 / image / merge_forward 等抽不到文本场景。
    try:
        _content_head = (msg.get("content") or "")[:200] if isinstance(msg.get("content"), str) else ""
        log.warning(
            "wave webhook in: event=%s msg_id=%s msg_type=%s quote=%s thread=%s "
            "chat=%s sender=%s/%s extracted_len=%s content_head=%r",
            event_type, wave_msg_id, media_type, parent_msg_id, thread_id,
            chat_id, sender_id, sender_id_type,
            len(extracted) if isinstance(extracted, str) else None,
            _content_head,
        )
    except Exception:  # noqa: BLE001
        log.exception("wave webhook in: diag log failed")

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

    # /clear 命令: 短路 LLM 链路, 只钉一条上下文起点, 不删数据。
    # cutoff = 当前最大 raw_id (含本条 /clear 自己), 之后 list_chat_history
    # 用 RawInput.id > cutoff 过滤; 老 raw 在 ingest 流水线里仍正常用。
    # 群里 /clear 群级生效, 私聊按 sender 域账号生效。
    if is_message_event and is_active_target and (extracted or "").strip() == "/clear":
        scope_key = chat_id if chat_id else (
            f"user:{sender_id}" if sender_id_type == "user_id" else ""
        )
        if scope_key:
            def _do_clear() -> None:
                with session() as sess:
                    cur_max = sess.query(raw_store.RawInput.id)\
                        .order_by(raw_store.RawInput.id.desc()).limit(1).scalar() or 0
                    raw_store.set_context_cutoff(sess, scope_key, int(cur_max))
            await asyncio.to_thread(_do_clear)
            from helper.im import wave_client
            try:
                if chat_id:
                    wave_client.send_message(
                        receiver_id=chat_id, receiver_id_type="chat_id",
                        msg_type="text", content={"text": "已清除当前对话上下文"},
                    )
                else:
                    wave_client.send_message(
                        receiver_id=sender_id, receiver_id_type=sender_id_type or "user_id",
                        msg_type="text", content={"text": "已清除当前对话上下文"},
                    )
            except Exception:  # noqa: BLE001
                log.exception("/clear send ack failed scope=%s", scope_key)
        return Response(content="", media_type="application/json")

    if is_message_event and is_active_target:
        # 主动 @bot / 单聊路径预先打 "skipped:ask_path" 标 — 防 backfill --force-all
        # 把这些 raw 当成"漏抽"重跑成 L1 (历史污染就是这么来的: ask 路径的 raw
        # 没人写 L1Result, backfill 看见 NULL 就拿去抽, 问句被新 prompt 抽成 section)。
        # 后续 intent 若判 judgment, process_raw 仍会覆盖此标继续跑 L1 (不阻塞主流程)。
        def _mark_ask_skipped() -> None:
            from helper.storage import session as _sess
            from helper.storage.models import L1Result
            with _sess() as s:
                if s.get(L1Result, raw_id) is None:
                    s.add(L1Result(
                        raw_id=raw_id,
                        error="skipped:ask_path",
                        model="ask_route",
                    ))
                    s.commit()
        await asyncio.to_thread(_mark_ask_skipped)
        # 用户正在跟 bot 说话,可能是问题也可能是指令(如"答哥别复述身份");
        # ask 与 memory 抽取并行 — ask 答这次问题,memory 抽下次起生效。
        from helper.memory import schedule_memory_extract
        schedule_memory_extract(raw_id)
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

    return Response(content="", media_type="application/json")
