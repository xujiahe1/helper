"""Raw input 的薄封装。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from helper.storage.models import RawInput

# 上下文窗口默认值 — intent classify / ask runtime / 后续需要"短期记忆"
# 的地方共用。8 条 / 1 小时,够覆盖一两轮承接,不让 prompt 膨胀。
CONTEXT_DEFAULT_LIMIT = 8
CONTEXT_DEFAULT_MINUTES = 60


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
) -> str:
    """拼一段「历史对话」标注块,供 intent classify / ask 共用。

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
