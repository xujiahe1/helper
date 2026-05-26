"""Conflict Detector v0 — LLM judge 检测新输入与已有 spec 的矛盾。

设计:
- 新 raw 落库 + L1 完成后,调用 detect_for_raw(raw_id)
- 检索 bundle 中相关 spec(关键词命中) → 喂 conflict_judge LLM
- LLM 判定 contradicts/refines/none:contradicts → 落 conflict_log,等仲裁
"""

from helper.conflict.detector import ConflictHit, detect_for_raw, resolve
from helper.conflict.notifier import run_notifications

__all__ = ["ConflictHit", "detect_for_raw", "resolve", "run_notifications"]
