"""Surface 2 — Inbox 周报。

每周一 push 一条卡片消息: 待 review 的 spec_candidates / 待裁决冲突 / 待答追问。
触发: cron / CLI manual / FastAPI 内部端点。
"""

from helper.inbox.weekly import WeeklyDigest, build_digest, render_card, send_to

__all__ = ["WeeklyDigest", "build_digest", "render_card", "send_to"]
