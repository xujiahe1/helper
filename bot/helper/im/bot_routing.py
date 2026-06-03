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


def _extract_raw_message(payload: dict[str, Any]) -> tuple[str, str]:
    """从 webhook 入站 payload 抽出对方 bot 原消息的 (msg_type, content_str)。

    content_str 是 Wave 协议要求的 JSON 字符串原文, 直接喂回 send_message 即可保真转发
    (card / rich_text 的所有视觉元素都能透传)。
    抽不到返 ("", "")。
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return "", ""
    msg = event.get("message")
    if not isinstance(msg, dict):
        return "", ""
    msg_type = msg.get("msg_type") if isinstance(msg.get("msg_type"), str) else ""
    content_str = msg.get("content") if isinstance(msg.get("content"), str) else ""
    return msg_type or "", content_str or ""


def _extract_reply_text(payload: dict[str, Any]) -> str:
    """fallback: 抽对方回复的纯文本(仅在原样转发失败时兜底用)。"""
    _, content_str = _extract_raw_message(payload)
    return _extract_markdown_from_inner(_safe_json(content_str)) if content_str else ""


def _safe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _walk_card_elements(elems: Any, out: list[str]) -> None:
    """递归走 card.elements,把 markdown / text / image / hr / 容器 元素抽成 markdown 段落。

    Wave card 的 elements 可能嵌套(layout 容器里又有 elements),所以递归。
    抽不出的组件(button / select / form / datepicker)忽略 — bot 间转发的视觉交互节点
    没法在 helper app 下复活,丢比报错好。
    """
    if not isinstance(elems, list):
        return
    for e in elems:
        if not isinstance(e, dict):
            continue
        tag = e.get("tag")
        if tag in ("markdown", "text", "plain_text", "lark_md"):
            t = e.get("text") or e.get("content")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
        elif tag == "image":
            url = e.get("image_url") or e.get("url")
            alt = e.get("alt") or e.get("title") or "图片"
            if isinstance(url, str) and url:
                out.append(f"![{alt}]({url})")
        elif tag == "hr":
            out.append("---")
        elif tag in ("note", "div"):
            # note / div 通常含子 elements 或 text
            t = e.get("text")
            if isinstance(t, dict):
                tt = t.get("content") or t.get("text")
                if isinstance(tt, str) and tt.strip():
                    out.append(tt.strip())
            elif isinstance(t, str) and t.strip():
                out.append(t.strip())
            _walk_card_elements(e.get("elements"), out)
        else:
            # column_set / form / 其他 layout: 递归找子 elements
            _walk_card_elements(e.get("elements"), out)
            _walk_card_elements(e.get("columns"), out)


def _extract_markdown_from_inner(inner: Any) -> str:
    """从 tachi 回的 inner content 抽出可读 markdown。覆盖:
      - 简单 text / rich_text(tags / items)
      - card(header.title + card.elements 含 markdown / image / hr / 嵌套容器)
      - i18n_card / i18n_text(优先 zh-cn → zh → en)
    抽不到返空串。
    """
    if not isinstance(inner, dict):
        return ""

    # 简单 text
    t = inner.get("text") or inner.get("content")
    if isinstance(t, str) and t.strip():
        return t.strip()

    # rich_text(at / text / url)
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
                elif tt == "url":
                    text = c.get("text") or c.get("url") or ""
                    url = c.get("url") or ""
                    if text and url:
                        chunks.append(f"[{text}]({url})")
                    elif url:
                        chunks.append(url)
                elif tt == "at":
                    name = c.get("user_name") or c.get("name") or c.get("id") or "user"
                    chunks.append(f"@{name}")
            chunks.append("\n")
        joined = "".join(chunks).strip()
        if joined:
            return joined

    # card(本期 tachi 主要走这条)
    parts: list[str] = []
    header = inner.get("header")
    if isinstance(header, dict):
        title = header.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(f"## {title.strip()}")
        elif isinstance(title, dict):
            tt = title.get("content") or title.get("text")
            if isinstance(tt, str) and tt.strip():
                parts.append(f"## {tt.strip()}")
        i18n_title = header.get("i18n_title")
        if not parts and isinstance(i18n_title, dict):
            for key in ("zh-cn", "zh", "en"):
                v = i18n_title.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(f"## {v.strip()}")
                    break

    card = inner.get("card")
    if isinstance(card, dict):
        _walk_card_elements(card.get("elements"), parts)

    # i18n_card.zh-cn.elements
    i18n_card = inner.get("i18n_card")
    if not parts and isinstance(i18n_card, dict):
        for key in ("zh-cn", "zh", "en"):
            sub = i18n_card.get(key)
            if isinstance(sub, dict):
                _walk_card_elements(sub.get("elements"), parts)
                if parts:
                    break

    # i18n_text(简单文本卡片)
    if not parts:
        i18n = inner.get("i18n_text")
        if isinstance(i18n, dict):
            for key in ("zh-cn", "zh", "en"):
                v = i18n.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()

    return "\n\n".join(p for p in parts if p).strip()


def _build_markdown_card_content(markdown_text: str) -> str:
    """用 helper 自己 app 能过 send 校验的最简 card json 字符串。

    只放 1 个 markdown element,不带 header / form / button — 避开 10401069 组件校验。
    """
    card_obj = {
        "card": {
            "tag": "flow",
            "elements": [
                {"tag": "markdown", "text": markdown_text, "text_align": "left"},
            ],
        }
    }
    return json.dumps(card_obj, ensure_ascii=False)


def _format_prefix(via_label: str, asker_domain: str = "") -> str:
    """转发前缀: 群聊里 @asker + "已咨询 @via:"; 私聊只 "已咨询 @via:"。"""
    if asker_domain:
        return f"@{asker_domain} 已咨询 @{via_label}:"
    return f"已咨询 @{via_label}:"


def _send_to_origin(
    *,
    chat_id: str,
    asker: str,
    wave_quote: str,
    msg_type: str,
    content_str: str,
) -> None:
    """把 (msg_type, content_str) 发回原会话: 群里优先 reply 原问题, 私聊发 asker。

    content_str 是 Wave 协议要求的 JSON 字符串原文, 直接喂给 wave_client 即可保真转发。
    wave_quote 传空串 → 强制走 send_message(避免重复 reply 同一条原问题)。
    """
    if chat_id:
        if wave_quote:
            wave_client.reply_message(wave_quote, msg_type=msg_type, content=content_str)
        else:
            wave_client.send_message(
                chat_id,
                receiver_id_type="chat_id",
                msg_type=msg_type,
                content=content_str,
            )
    elif asker:
        wave_client.send_message(
            asker,
            receiver_id_type="user_id",
            msg_type=msg_type,
            content=content_str,
        )


_RESTATE_SYSTEM_PROMPT = """你正在帮 helper bot 把外部 bot 的回答重新组织成 markdown 卡片。

输入是一个外部 bot 给用户问题的回复, 已经抽成纯文本(可能扁平、表格被压成空格分隔行、
段落分隔丢失)。你的任务: 把它整理成结构清晰的 markdown, 让用户看着舒服。

要求:
- 用 markdown 自由表达: 标题、列表、表格、代码块都可以, 不要分段标题脚手架(像 ## 答复)
- 内容重新组织, 不必逐字保留措辞 — 但所有事实细节必须原样保留:
  * 链接 URL、API 路径、HTTP 方法、参数名、字段名、返回码、Cookie 名等技术 token 不允许改写
  * 数字、人名、bot 名、产品名不允许改写
  * 不要补任何原文里没有的事实
- 不要加"以下是..."、"我来重新整理..."这种引导语, 直接给重写后的内容
- 不要加 ## 来自 X 的回答 之类的 header — helper 已经有专门的咨询前缀卡片
- 输出就是给用户看的最终内容, 不要任何外层包装、不要 fence"""


def _restate_to_markdown(extracted_text: str, via_label: str) -> str:
    """走 restate_bot_reply task 把对方扁平输出重写成 markdown。失败返空串。"""
    if not extracted_text.strip():
        return ""
    from helper.llm import run
    user = f"外部 bot 是 @{via_label}, 它给用户问题的回复(已抽成纯文本):\n\n{extracted_text}"
    try:
        out = run("restate_bot_reply", system=_RESTATE_SYSTEM_PROMPT, user=user, temperature=0.2)
    except Exception as e:  # noqa: BLE001
        log.warning("restate_bot_reply LLM failed: %s", e)
        return ""
    return out.strip()


def handle_bot_reply(payload: dict[str, Any], *, sender_app_id: str) -> bool:
    """target bot 私聊回 helper → 找最近未消费 PendingRouting → 前缀 + LLM 转述卡片 + 标 consumed。

    流程:
      a) 前缀("@asker 已咨询 @via:") → 优先替换 tracker 卡片, 否则发一条 text
      b) 抽对方原文 → LLM 转述 → markdown card 发到原会话
      c) 转述失败 / 抽不到原文 → 兜底抽 text 直发

    返回 True = 关联到一条 routing 并回贴; False = 没找到 routing(应丢弃, 不走 raw_inputs)。

    历史: b) 早期是"原样透传"(直接把对方 card content 喂回 send_message),
    但对方 card 里的 form/button/select 组件在 helper app 下过不了 Wave 组件校验
    (retcode 10401069); 退一步用 walker 抽 markdown 重构最简 card 又会拿到 tachi
    端已经被压扁的 i18n_text(表格 → 空格分隔单行, 段落 → 连续无 \\n)。最后落到
    LLM 转述: 由 restate_bot_reply task 重新组织成结构清晰的 markdown, 用 helper
    自己 app 的最简 markdown card 出。
    """
    if not sender_app_id:
        return False

    msg_type, content_str = _extract_raw_message(payload)
    # 现场取证: 落对方 bot 原始消息体, 用于排查格式还原问题(walker 抽不全 / column_set
    # 表格丢结构 / 图片走 fallback 之类)。content_str 是 Wave 协议 JSON 字符串原文,
    # 完整保真。WARNING 级别确保 uvicorn root logger 默认配置下也能落到 helper.err,
    # 不依赖 helper 自定义 log config (不存在)。只在出格式问题时翻, 不需要长期保留。
    log.warning(
        "bot reply payload sender=%s msg_type=%s content=%s",
        sender_app_id, msg_type, content_str,
    )
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

    prefix = _format_prefix(via_label=via, asker_domain=asker if chat_id else "")

    # a) 前缀: 优先替换 thinking 卡片(用户感知一气呵成), 否则单发一条 text
    prefix_via_card = False
    if tracker_card:
        try:
            wave_client.update_card_active(
                tracker_card,
                content={
                    "card": {
                        "tag": "flow",
                        "elements": [
                            {"tag": "markdown", "text": prefix, "text_align": "left"},
                        ],
                    }
                },
            )
            prefix_via_card = True
        except WaveAPIError as e:
            log.warning("update_card_active failed card=%s: %s", tracker_card, e)

    if not prefix_via_card:
        try:
            _send_to_origin(
                chat_id=chat_id,
                asker=asker,
                wave_quote=wave_quote,
                msg_type="text",
                content_str=json.dumps({"text": prefix}, ensure_ascii=False),
            )
        except WaveAPIError as e:
            log.warning("send prefix failed routing=%d: %s", routing_id, e)

    # b) 抽对方原文 → LLM 转述 → markdown card 发
    extracted = _extract_markdown_from_inner(_safe_json(content_str)) if content_str else ""
    forwarded = False
    if extracted:
        restated = _restate_to_markdown(extracted, via)
        # 现场取证: 落 LLM 转述出来的原始 markdown 文本和实际发到 wave 的 card content_str。
        # 用来排查"卡片渲染扁平"问题: LLM 是否按 Wave 卡片 markdown 语法 (| 表格 | / # 标题 / ```代码块```)
        # 输出, 还是给了平铺的伪结构。WARNING 级别走 helper.err 默认 root logger。
        # 只在排查格式问题时翻, 不需要长期保留。
        log.warning(
            "restate output routing=%d via=%s restated=%r",
            routing_id, via, restated,
        )
        if restated:
            card_content = _build_markdown_card_content(restated)
            log.warning(
                "outbound restated card routing=%d content=%s",
                routing_id, card_content,
            )
            try:
                _send_to_origin(
                    chat_id=chat_id,
                    asker=asker,
                    wave_quote="",
                    msg_type="card",
                    content_str=card_content,
                )
                forwarded = True
            except WaveAPIError as e:
                log.warning(
                    "forward restated card failed routing=%d: %s",
                    routing_id, e,
                )

    # c) 转述失败 / 抽不到原文 → 抽 text 兜底直发
    if not forwarded:
        fallback_text = extracted or _extract_reply_text(payload) or "(对方回了, 但内容抽不到, 请直接看会话)"
        try:
            _send_to_origin(
                chat_id=chat_id,
                asker=asker,
                wave_quote="",
                msg_type="text",
                content_str=json.dumps({"text": fallback_text}, ensure_ascii=False),
            )
        except WaveAPIError as e:
            log.warning("forward fallback text failed routing=%d: %s", routing_id, e)

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
