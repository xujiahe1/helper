"""统一冲突 detector — 5 类 L1 原子(decision/fact/case/concept/relation)
对应已有候选都跑冲突检测,落到同一张 conflict_log。

设计:
- 每条 raw 落库 + L1 完成后,sink._run_consumers 自动调用 detect_for_raw(raw_id)
- decision 走 LLM judge(conflict_judge),其它走结构化判定
- 用 owner 通过 inbox 周报或主动 /inbox 用「采纳/保留/都留 2-N」裁决

冲突推送复用 inbox 周报(weekly digest 已含 open conflicts 列表),不另起 notifier。
多 owner 时再设计专门的通知路径。
"""

from helper.conflict.detector import ConflictHit, detect_for_raw, resolve

__all__ = ["ConflictHit", "detect_for_raw", "resolve"]
