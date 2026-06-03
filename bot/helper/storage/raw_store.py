"""Raw input 的薄封装。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from helper.storage.models import ChatContextCutoff, RawInput


def _scope_key(chat_id: str, fallback_author: str) -> str:
    """list_chat_history 与 /clear 共用的 scope 键: 群=chat_id, 私聊=user:<domain>。"""
    if chat_id:
        return chat_id
    if fallback_author:
        return f"user:{fallback_author}"
    return ""


def get_context_cutoff(s: Session, scope_key: str) -> int:
    if not scope_key:
        return 0
    row = s.get(ChatContextCutoff, scope_key)
    return row.cutoff_raw_id if row else 0


def set_context_cutoff(s: Session, scope_key: str, cutoff_raw_id: int) -> None:
    """upsert: /clear 触发时把当前最大 raw_id 钉作起点。"""
    if not scope_key:
        return
    row = s.get(ChatContextCutoff, scope_key)
    now = datetime.now(timezone.utc)
    if row is None:
        s.add(ChatContextCutoff(
            scope_key=scope_key, cutoff_raw_id=cutoff_raw_id, updated_at=now,
        ))
    else:
        row.cutoff_raw_id = cutoff_raw_id
        row.updated_at = now

# 上下文窗口默认值 — intent classify / ask runtime / 后续需要"短期记忆"
# 的地方共用。16 条 / 1 天,覆盖跨日承接但不让 prompt 失控。
CONTEXT_DEFAULT_LIMIT = 16
CONTEXT_DEFAULT_MINUTES = 24 * 60


def append(
    s: Session,
    *,
    source_type: str,
    content_text: str,
    source_ref: str = "",
    author_domain: str = "",
    attachments_json: str = "[]",
    # ---- Wave IM 上下文 ----
    chat_id: str = "",
    is_at_bot: bool = False,
    forward_from_user: str = "",
    forward_from_message_id: str = "",
    parent_message_id: str = "",
    thread_id: str = "",
    media_type: str = "",
    wave_message_id: str = "",
) -> RawInput:
    """落 raw_inputs。对 IM 来源(wave_message_id 非空)幂等:
    若 (chat_id, wave_message_id) 已在表里就直接返回旧行,不重复插入。

    Wave webhook 重投 / 跨进程并发 / 未来主动拉历史补漏 都靠这里兜底,
    DB 层还有 uq_raw_inputs_im_msg 部分唯一索引最终防线。
    """
    if wave_message_id:
        existing = (
            s.query(RawInput)
            .filter(
                RawInput.chat_id == chat_id,
                RawInput.wave_message_id == wave_message_id,
            )
            .first()
        )
        if existing is not None:
            return existing

    row = RawInput(
        source_type=source_type,
        source_ref=source_ref,
        author_domain=author_domain,
        content_text=content_text,
        attachments_json=attachments_json,
        chat_id=chat_id,
        is_at_bot=is_at_bot,
        forward_from_user=forward_from_user,
        forward_from_message_id=forward_from_message_id,
        parent_message_id=parent_message_id,
        thread_id=thread_id,
        media_type=media_type,
        wave_message_id=wave_message_id,
    )
    s.add(row)
    s.flush()
    return row


def list_recent(s: Session, limit: int = 20) -> list[RawInput]:
    return list(s.query(RawInput).order_by(RawInput.id.desc()).limit(limit).all())


def get_by_wave_msg_id(
    s: Session, wave_msg_id: str, *, chat_id: str | None = None
) -> RawInput | None:
    """按 Wave 协议 msg_id 反查 raw 行。私聊 chat_id 是空, 群聊不空 — 调用方传 None
    表示不约束 chat_id(场景: 反查"用户引用的消息", 引用对象可能是 bot 自己更早的私聊回复)。

    Wave msg_id 全平台唯一, 即便 chat_id 不约束也不会跨会话误命中。"""
    if not wave_msg_id:
        return None
    q = s.query(RawInput).filter(RawInput.wave_message_id == wave_msg_id)
    if chat_id is not None:
        q = q.filter(RawInput.chat_id == chat_id)
    return q.order_by(RawInput.id.desc()).first()


def list_chat_history(
    s: Session,
    chat_id: str,
    *,
    limit: int = 30,
    since_days: int = 3,
    since_minutes: int | None = None,
    exclude_raw_id: int | None = None,
    fallback_author: str = "",
) -> list[RawInput]:
    """拉某个会话的近期消息。返回按时间正序(老→新)。

    用途:
    - ask / intent 拼对话上下文
    - L1 抽取拉"被 @bot 那条 + 上下文窗口"作为整体素材
      (传 since_minutes=30, limit=20 — 只要紧邻同话题的几条)

    匹配规则:
    - chat_id 非空 → 按 chat_id 精确匹配(群聊场景)
    - chat_id 为空 + fallback_author 非空 → 单聊兜底:拉 author_domain == fallback_author
      且 chat_id == "" 的消息 + bot 回给该 author 的回复(source_type=im_wave_bot 且
      author_domain 记录的是接收方域账号)
    - 都为空 → 返空列表
    - since_minutes 优先于 since_days
    - exclude_raw_id 把当前那条排除
    - 仅 IM 来源(source_type 以 im_wave 开头)
    """
    if since_minutes is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    q = s.query(RawInput).filter(
        RawInput.created_at >= cutoff.replace(tzinfo=None),
        RawInput.source_type.like("im_wave%"),
    )
    if chat_id:
        q = q.filter(RawInput.chat_id == chat_id)
    elif fallback_author:
        # 单聊:用户消息(author_domain=该用户, chat_id="") + bot 回复(对该用户)
        q = q.filter(
            RawInput.chat_id == "",
            RawInput.author_domain == fallback_author,
        )
    else:
        return []
    if exclude_raw_id is not None:
        q = q.filter(RawInput.id != exclude_raw_id)
    # /clear 起点: 老于 cutoff 的 raw 不再加载到 prompt(数据本身保留)
    cutoff = get_context_cutoff(s, _scope_key(chat_id, fallback_author))
    if cutoff:
        q = q.filter(RawInput.id > cutoff)
    q = q.order_by(RawInput.id.desc()).limit(limit)
    rows = list(q.all())
    rows.reverse()  # 时间正序
    return rows


def format_context_block(
    s: Session,
    *,
    chat_id: str,
    fallback_author: str = "",
    exclude_raw_id: int | None = None,
    limit: int = CONTEXT_DEFAULT_LIMIT,
    since_minutes: int = CONTEXT_DEFAULT_MINUTES,
    max_chars_per_line: int = 200,
    asker_domain: str = "",
) -> str:
    """拼一段「历史对话」标注块,供 intent classify / ask 共用。

    asker_domain 非空时按 ACL 过滤: 非白名单 asker 看到的群历史里,
    带 acl_topic_id 标的 raw 直接跳过(像那条消息没发生过)— 避免
    白名单用户的敏感聊天被穿插进群的非白名单 asker 通过 chat_context 看到。
    asker_domain 留空时不过滤(向后兼容,如 L1 抽取等内部使用场景)。

    返回示例:
        ## 历史对话(仅供你理解当前消息的指代/承接,不要被它带偏)
        [05-28 18:20] 用户(jiahe.xu): IAM-新可见性模块... 去学一下
        [05-28 18:20] bot: ❌ 我没权限读这篇文档...
        [05-28 18:23] 用户(jiahe.xu): 再读一下试试

    没有历史 → 返空串。调用方自己拼到 prompt 里。
    """
    rows = list_chat_history(
        s,
        chat_id,
        limit=limit,
        since_minutes=since_minutes,
        exclude_raw_id=exclude_raw_id,
        fallback_author=fallback_author,
    )
    if not rows:
        return ""
    if asker_domain:
        try:
            from helper.acl import current_acl
            acl = current_acl()
            if acl.topics:
                rows = [
                    r for r in rows
                    if acl.is_allowed(asker_domain, getattr(r, "acl_topic_id", "") or "")
                ]
        except Exception:  # noqa: BLE001
            # ACL 加载失败时不过滤(向 ask 主路径报告 LLM 决定 fail-open),
            # 但 ask runtime 的 deny_for_question 仍是首道闸。
            pass
    if not rows:
        return ""
    lines = ["## 历史对话(仅供你理解当前消息的指代/承接,不要被它带偏)"]
    for r in rows:
        ts = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "?"
        if (r.source_type or "").startswith("im_wave_bot"):
            who = "bot"
        else:
            who = f"用户({r.author_domain or '?'})"
        body = (r.content_text or "").strip().replace("\n", " ")
        if len(body) > max_chars_per_line:
            body = body[:max_chars_per_line] + "…"
        lines.append(f"[{ts}] {who}: {body}")
    return "\n".join(lines)
