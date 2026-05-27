"""Conflict Detector v0 — LLM judge 检测新输入与已有 spec 的矛盾。

设计:
- 新 raw 落库 + L1 完成后,调用 detect_for_raw(raw_id)
- 检索 bundle 中相关 spec(关键词命中) → 喂 conflict_judge LLM
- LLM 判定 contradicts/refines/none:contradicts → 落 conflict_log,等仲裁

冲突推送复用 inbox 周报(weekly digest 已含 open conflicts 列表),不另起 notifier。
多 owner 时再设计专门的通知路径。
"""

from helper.conflict.detector import ConflictHit, detect_for_raw, resolve

__all__ = ["ConflictHit", "detect_for_raw", "resolve"]
