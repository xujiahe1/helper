"""Raw input 的薄封装。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from helper.storage.models import RawInput


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
) -> list[RawInput]:
    """拉某个会话的近期消息。返回按时间正序(老→新)。

    用途:
    - ask 拼对话上下文(默认 since_days=3, limit=30)
    - L1 抽取拉"被 @bot 那条 + 上下文窗口"作为整体素材
      (传 since_minutes=30, limit=20 — 只要紧邻同话题的几条)

    - chat_id 为空(私聊)直接返空列表
    - since_minutes 优先于 since_days(传了就走分钟,否则按天)
    - exclude_raw_id:把当前正在处理的那条排除
    - 仅 IM 来源(source_type 以 im_wave 开头)
    """
    if not chat_id:
        return []
    if since_minutes is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    q = s.query(RawInput).filter(
        RawInput.chat_id == chat_id,
        RawInput.created_at >= cutoff.replace(tzinfo=None),
        RawInput.source_type.like("im_wave%"),
    )
    if exclude_raw_id is not None:
        q = q.filter(RawInput.id != exclude_raw_id)
    q = q.order_by(RawInput.id.desc()).limit(limit)
    rows = list(q.all())
    rows.reverse()  # 时间正序
    return rows
