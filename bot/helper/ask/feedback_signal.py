"""把 ReactionLog 聚合成 retrieve 排序的加权信号。

链路:ReactionLog.msg_id → AskAnswer.wave_msg_id → AskAnswer.citations_json
     citations_json 形如 [{"type":"spec","ref":"..."}, {"type":"raw","ref":"123"}]
聚合输出 {(type, ref): weight_delta},retrieve.py 在 RRF 融合后加上去。

设计取舍:
  - dislike 比 like 权重更大(踩稀有,信号更强)
  - reaction emoji 白名单只认明确的正负向(thumbsup/thumbsdown 等),其他视为中性 → 0
  - 30 天前的反馈 ×0.5 衰减,避免远古信号支配当下排序
  - cancel_like / cancel_dislike / 任何 reaction_deleted:* → 该 (operator, msg) 不贡献
  - 同一条 (operator, msg) 因为 ReactionLog 是覆盖更新,这里读到的就是最终态,不需要去重
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from helper.storage import session
from helper.storage.models import AskAnswer, ReactionLog

log = logging.getLogger(__name__)


# action_type → 单次贡献(衰减前)
_FEEDBACK_WEIGHTS: dict[str, float] = {
    "like": 0.2,
    "dislike": -0.3,
    # cancel_* 不贡献(已被覆盖更新写成此值,说明用户撤销了)
    "cancel_like": 0.0,
    "cancel_dislike": 0.0,
}

# emoji_type → 贡献(用 reaction:<emoji> 前缀匹配)
_REACTION_EMOJI_WEIGHTS: dict[str, float] = {
    "thumbsup": 0.1,
    "heart": 0.1,
    "fire": 0.1,
    "100": 0.1,
    "ok": 0.1,
    "good": 0.1,
    "yes": 0.1,
    "thumbsdown": -0.2,
    "x": -0.2,
    "no": -0.2,
}

DECAY_AFTER_DAYS = 30
DECAY_FACTOR = 0.5


def _action_weight(action_type: str) -> float:
    """把 action_type 字符串映射成单次贡献。未识别的返 0。"""
    if action_type in _FEEDBACK_WEIGHTS:
        return _FEEDBACK_WEIGHTS[action_type]
    if action_type.startswith("reaction_deleted:"):
        return 0.0
    if action_type.startswith("reaction:"):
        emoji = action_type.split(":", 1)[1]
        return _REACTION_EMOJI_WEIGHTS.get(emoji, 0.0)
    return 0.0


def _parse_citations(j: str | None) -> list[tuple[str, str]]:
    """citations_json → [(type, ref), ...]。解析失败返 []。"""
    try:
        items = json.loads(j or "[]")
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        r = it.get("ref")
        if isinstance(t, str) and t and r is not None:
            out.append((t, str(r)))
    return out


def feedback_weights() -> dict[tuple[str, str], float]:
    """扫 ReactionLog 全表 join AskAnswer,返 {(type, ref): summed_delta}。

    单条反馈贡献:`_action_weight(action_type) * decay`,其中 decay=0.5 if 30 天前 else 1.0。
    多个用户对同一 spec/raw 的反馈累加。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=DECAY_AFTER_DAYS)
    out: dict[tuple[str, str], float] = {}
    with session() as s:
        rows = s.execute(
            select(ReactionLog, AskAnswer)
            .join(AskAnswer, AskAnswer.id == ReactionLog.related_ask_id)
        ).all()
        for rl, ask in rows:
            base = _action_weight(rl.action_type or "")
            if base == 0.0:
                continue
            action_time = rl.action_time
            # sqlite 拿出来的 datetime 可能 naive,补 utc 以便比较
            if action_time is not None and action_time.tzinfo is None:
                action_time = action_time.replace(tzinfo=timezone.utc)
            decay = DECAY_FACTOR if action_time and action_time < cutoff else 1.0
            delta = base * decay
            for key in _parse_citations(ask.citations_json):
                out[key] = out.get(key, 0.0) + delta
    return out
