"""Wave webhook 入站后的副作用 — 全部后台 fire-and-forget。

Wave 1s 回调窗口里塞不下两次 Wave 开放平台 API 调用(access_token + 实际接口),
所以反查域账号 / 发 ack 回执都丢到后台异步跑。

入口: schedule_post_message(raw_id, sender_id, sender_id_type, *, send_ack=True)
- 反查域账号 → 写 IdentityCache + 回填 raw_inputs.author_domain
- ack 回执("已记录")给用户

每个动作独立失败,不互相阻塞;每个动作的失败都只 warning 不抛(后台任务里抛了
没人接,只会被 asyncio 默默吞掉)。
"""

from __future__ import annotations

import asyncio
import logging

from helper.im import wave_client
from helper.im.queue import llm_slot, spawn
from helper.im.wave_client import WaveAPIError
from helper.storage import session
from helper.storage.models import AskAnswer, IdentityCache, RawInput

log = logging.getLogger(__name__)


# ---------- 域账号 + 姓名反查 + IdentityCache ----------

def resolve_identity(sender_id: str, sender_id_type: str) -> tuple[str, str]:
    """根据 sender_id_type 拿 (域账号, 姓名)。失败返 ("", "") ,不抛。

    优先级:
      - user_id(域账号已有) → 直接返,顺带打 users/get 拿姓名(命中 cache 就免了)
      - union_id(ou_xxx)   → users/get 一把梭出 user_id + name
      - 其它(open_id 等)   → 跳过(没有官方反查路径)

    IdentityCache 用 sender_id 作主键(跟入站 sender 的 id 字段对齐)。
    """
    if not sender_id:
        return "", ""

    # 1) 命中 cache 直接返
    with session() as s:
        cached = s.get(IdentityCache, sender_id)
        if cached and cached.domain_account:
            return cached.domain_account, cached.name or ""

    if sender_id_type not in ("user_id", "union_id"):
        return "", ""

    # 2) 调 users/get 一把拿域账号 + 姓名
    try:
        users = wave_client.get_users_info([sender_id], uid_type=sender_id_type)
    except WaveAPIError as e:
        log.warning("users/get failed for %s (%s): %s", sender_id, sender_id_type, e)
        return "", ""
    if not users:
        log.info("users/get returned empty for %s", sender_id)
        return "", ""

    u = users[0]
    domain = u.get("user_id") or ""
    name = u.get("name") or ""
    if not domain:
        return "", name

    # 3) 写 cache
    with session() as s:
        existing = s.get(IdentityCache, sender_id)
        if existing:
            existing.domain_account = domain
            if name:
                existing.name = name
        else:
            s.add(
                IdentityCache(
                    wave_user_id=sender_id, domain_account=domain, name=name
                )
            )
    return domain, name


def resolve_domain_account(sender_id: str, sender_id_type: str) -> str:
    """向后兼容的旧接口,只要域账号。"""
    domain, _ = resolve_identity(sender_id, sender_id_type)
    return domain


def _backfill_author_domain(raw_id: int, domain: str) -> None:
    if not domain:
        return
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return
        # 仅当当前 author_domain 还是 sender_id(未反查过)时回填,避免覆盖手填值
        if raw.author_domain == domain:
            return
        raw.author_domain = domain


# ---------- ack 回执 ----------

def send_ack(receiver_id: str, receiver_id_type: str, raw_id: int) -> None:
    """给用户回一条"已记录"。失败只 warning 不抛(回执失败不影响主链路)。

    receiver_id_type 必须是 send_message 接受的合法值(union_id / user_id / chat_id)。
    open_id 不行,直接跳过。
    """
    if not receiver_id or receiver_id_type not in ("user_id", "union_id", "chat_id"):
        log.info("skip ack: id_type=%s not supported by send_message", receiver_id_type)
        return
    try:
        wave_client.send_message(
            receiver_id,
            msg_type="text",
            content={"text": "✓ 已记录"},
            receiver_id_type=receiver_id_type,
            send_type=1,
        )
    except WaveAPIError as e:
        log.warning("send ack failed raw#%d -> %s/%s: %s", raw_id, receiver_id_type, receiver_id, e)


# ---------- 编排 + fire-and-forget ----------

def _post_message_sync(
    raw_id: int, sender_id: str, sender_id_type: str, *, ack: bool
) -> None:
    domain = resolve_domain_account(sender_id, sender_id_type)
    _backfill_author_domain(raw_id, domain)
    if ack:
        send_ack(sender_id, sender_id_type, raw_id)


async def _post_message_async(
    raw_id: int, sender_id: str, sender_id_type: str, *, ack: bool
) -> None:
    try:
        await asyncio.to_thread(
            _post_message_sync, raw_id, sender_id, sender_id_type, ack=ack
        )
    except Exception:  # noqa: BLE001
        log.exception("post-message background task failed raw#%d", raw_id)


def schedule_post_message(
    raw_id: int,
    sender_id: str,
    sender_id_type: str,
    *,
    send_ack: bool = True,
) -> None:
    """fire-and-forget: 后台跑域账号反查 + ack 回执。

    - 在 event loop 里 → spawn 带强引用入 _pending(webhook 路径)
    - 没 loop → 同步跑(测试 / CLI 场景)
    - sender_id 为空直接 noop
    """
    if not sender_id:
        return
    task = spawn(_post_message_async(raw_id, sender_id, sender_id_type, ack=send_ack))
    if task is None:
        _post_message_sync(raw_id, sender_id, sender_id_type, ack=send_ack)


# ---------- 通用 reply 工具 ----------

def _reply_text(wave_msg_id: str, text: str, *, request_id: str = "") -> None:
    """对一条 wave 消息回纯文本。失败只 warning。"""
    if not wave_msg_id or not text:
        return
    try:
        wave_client.reply_message(
            msg_id=wave_msg_id,
            msg_type="text",
            content={"text": text},
            request_id=request_id or None,
        )
    except WaveAPIError as e:
        log.warning("reply_text failed msg=%s: %s", wave_msg_id, e)


# ---------- Ask 路径(intent classify → reply with citations) ----------

_INBOX_TRIGGERS = {
    "/inbox", "inbox", "/digest", "digest",
    "周报", "看周报", "我的周报", "立刻给我看周报",
    "我的 inbox", "我的inbox",
}


def _is_inbox_trigger(text: str) -> bool:
    """owner 私聊里发的 owner 主动触发 — 命中即立刻 build + 推 + 快照。

    简短指令文匹配,不走 LLM intent。命中条件: 整条消息(strip)等于
    任何一个触发词。避免「我刚发的判断里提了 /inbox」被误命中。
    """
    t = (text or "").strip().lower()
    return t in {x.lower() for x in _INBOX_TRIGGERS}


def _route_message_sync(
    *,
    raw_id: int,
    text: str,
    sender_id: str,
    sender_id_type: str,
    chat_id: str,
    wave_msg_id: str,
) -> None:
    """主路径: 反查身份 → intent classify → judgment 走 L1+ack / ask 走 Ask runtime + 回复。

    全后台跑,任何一步出错只 log,不影响其他。
    """
    from helper.ask import ask
    from helper.ask.runtime import render_for_wave
    from helper.im import km_ingest
    from helper.im.intent import classify
    from helper.ingest import process_raw, schedule_l1

    from helper.scheduler import (
        get_pending_confirm,
        handle_cancel,
        handle_confirm,
        handle_create,
        handle_list,
    )

    domain, _name = resolve_identity(sender_id, sender_id_type)
    _backfill_author_domain(raw_id, domain)

    # 群里发消息,reply 用 chat_id;单聊用 sender 自身
    if chat_id:
        receiver_id, receiver_id_type = chat_id, "chat_id"
    else:
        receiver_id, receiver_id_type = sender_id, sender_id_type

    # 优先级 -1: 消息里含 KM 链接 → 整篇文档拉成 raw + 跑 L1。
    # @bot 分享 KM 文档当作"喂知识",不再走 intent 分类(避免被 ask 当成问题)。
    km_urls = km_ingest.find_km_urls(text)
    if km_urls:
        try:
            results = km_ingest.ingest_text(
                text,
                sender_domain=domain or sender_id,
                chat_id=chat_id,
                parent_message_id=wave_msg_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("km_ingest failed raw#%d urls=%s", raw_id, km_urls)
            _reply_text(wave_msg_id, "⚠️ KM 文档拉取异常,稍后重试", request_id=f"km-err-{raw_id}")
            return
        # 成功拉到的 → 各自跑 L1(进 sink → 4 类候选)
        for r in results:
            if r.status == "ok" and r.raw_id:
                schedule_l1(r.raw_id)
        reply = km_ingest.format_results(results)
        if reply:
            _reply_text(wave_msg_id, reply, request_id=f"km-ack-{raw_id}")
        return

    # 优先级 0a: owner 私聊里的 inbox 回执(批准/驳回/跳过 1-N + 采纳/保留/都留 2-N + 答 3-N ...)
    # 在 schedule_confirm 之前,避免「批准 1-3」被误绑到无关 confirm
    if domain and not chat_id:
        # 「/inbox」/「inbox」/「周报」/「立刻给我看周报」等 — owner 主动触发当下 digest
        if _is_inbox_trigger(text):
            from helper.config import get_settings
            from helper.inbox import build_digest, render_card, snapshot_digest

            owner = get_settings().helper_owner_domain
            if owner and domain == owner:
                d = build_digest()
                body = render_card(d)
                snapshot_digest(domain, d)
                _reply_text(wave_msg_id, body, request_id=f"inbox-now-{raw_id}")
                return

        from helper.inbox import try_handle_reply

        inbox_reply = try_handle_reply(
            text, sender_domain=domain, chat_id=chat_id, answer_raw_id=raw_id,
        )
        if inbox_reply is not None:
            _reply_text(wave_msg_id, inbox_reply.text, request_id=f"inbox-reply-{raw_id}")
            for action_name, payload in inbox_reply.after_actions:
                if action_name == "schedule_l1":
                    schedule_l1(payload)
            return

    # 优先级 0b: 该用户有 pending schedule confirm → 当前消息一律视为对它的回应
    if domain and get_pending_confirm(domain) is not None:
        reply = handle_confirm(text, domain)
        if reply is not None:
            _reply_text(wave_msg_id, reply, request_id=f"sched-confirm-{raw_id}")
            return

    try:
        intent = classify(text)
    except Exception as e:  # noqa: BLE001
        log.warning("intent classify failed raw#%d: %s", raw_id, e)
        intent = "judgment"

    # 优先级 1: schedule_* 三类直接走 scheduler handler
    if intent == "schedule_create":
        reply = handle_create(text, domain)
        _reply_text(wave_msg_id, reply, request_id=f"sched-create-{raw_id}")
        return
    if intent == "schedule_list":
        reply = handle_list(domain)
        _reply_text(wave_msg_id, reply, request_id=f"sched-list-{raw_id}")
        return
    if intent == "schedule_cancel":
        reply = handle_cancel(text, domain)
        _reply_text(wave_msg_id, reply, request_id=f"sched-cancel-{raw_id}")
        return

    if intent == "ask":
        try:
            ans = ask(
                text,
                asker_domain=domain or sender_id,
                chat_id=chat_id,
                raw_id=raw_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("ask runtime failed raw#%d", raw_id)
            return
        body = render_for_wave(ans)
        # 用 reply API,带 feedback_config 让用户能 👍/👎
        try:
            resp = wave_client.reply_message(
                msg_id=wave_msg_id,
                msg_type="text",
                content={"text": body},
                enable_feedback=True,
                request_id=f"ask-{ans.answer_id}" if ans.answer_id else f"ask-raw{raw_id}",
            )
        except WaveAPIError as e:
            log.warning("reply ask failed raw#%d: %s", raw_id, e)
            return
        # 把 bot 回复的 wave_msg_id 回填到 ask_answers,方便 reaction 反查
        bot_msg_id = ""
        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            bot_msg_id = data.get("message_id") or data.get("msg_id") or ""
        if bot_msg_id and ans.answer_id is not None:
            with session() as s:
                row = s.get(AskAnswer, ans.answer_id)
                if row is not None:
                    row.wave_msg_id = bot_msg_id
                    s.commit()
        return

    # judgment / other → L1 + ack
    if intent == "judgment":
        process_raw(raw_id)
    if receiver_id and receiver_id_type in ("user_id", "union_id", "chat_id"):
        try:
            wave_client.send_message(
                receiver_id,
                msg_type="text",
                content={"text": "✓ 已记录"},
                receiver_id_type=receiver_id_type,
                send_type=1,
            )
        except WaveAPIError as e:
            log.warning("ack send failed raw#%d: %s", raw_id, e)


def schedule_ask_reply(
    *,
    raw_id: int,
    text: str,
    sender_id: str,
    sender_id_type: str,
    chat_id: str,
    wave_msg_id: str,
) -> None:
    """webhook 用: 后台跑 intent classify + Ask 回复或 L1+ack。

    所有 LLM 调用走 llm_slot semaphore 限流(默认 5 路并发上限),
    避免高并发(群里多人同时 @bot)打爆 Athenai 限流。
    """
    if not sender_id:
        return
    task = spawn(
        _route_message_with_slot(
            raw_id=raw_id,
            text=text,
            sender_id=sender_id,
            sender_id_type=sender_id_type,
            chat_id=chat_id,
            wave_msg_id=wave_msg_id,
        )
    )
    if task is None:
        _route_message_sync(
            raw_id=raw_id,
            text=text,
            sender_id=sender_id,
            sender_id_type=sender_id_type,
            chat_id=chat_id,
            wave_msg_id=wave_msg_id,
        )


async def _route_message_with_slot(**kwargs) -> None:
    """包 semaphore — 只有拿到 slot 才进 _route_message_sync(里面会调多个 LLM)。"""
    async with llm_slot():
        try:
            await asyncio.to_thread(_route_message_sync, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("ask-route background failed raw#%d", kwargs.get("raw_id", -1))
