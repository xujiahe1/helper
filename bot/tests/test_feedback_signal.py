"""feedback_signal — ReactionLog 聚合权重 + retrieve.py 加权排序回归。

链路:
  ReactionLog (msg_id, action_type) → AskAnswer.wave_msg_id → AskAnswer.citations_json
  → 聚合到 (type, ref) → retrieve_relevant 排序时加 delta
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def _add_ask(wave_msg_id: str, citations: list[dict]) -> int:
    from helper.storage import session
    from helper.storage.models import AskAnswer

    with session() as s:
        a = AskAnswer(
            asker_domain="alice",
            question="q",
            answer="a",
            citations_json=json.dumps(citations),
            wave_msg_id=wave_msg_id,
        )
        s.add(a)
        s.flush()
        return a.id


def _add_reaction(operator_id: str, msg_id: str, action_type: str,
                  related_ask_id: int, *, days_ago: int = 0) -> None:
    from helper.storage import session
    from helper.storage.models import ReactionLog

    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with session() as s:
        s.add(ReactionLog(
            operator_id=operator_id,
            msg_id=msg_id,
            operator_id_type="union_id",
            operator_user_id="u",
            action_type=action_type,
            related_ask_id=related_ask_id,
            action_time=when,
        ))


def test_dislike_produces_negative_weight(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_bot_1", [{"type": "spec", "ref": "bad-spec"}])
    _add_reaction("ou_a", "om_bot_1", "dislike", ask_id)

    weights = feedback_weights()
    assert weights[("spec", "bad-spec")] < 0


def test_like_produces_positive_weight(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_bot_2", [{"type": "spec", "ref": "good-spec"}])
    _add_reaction("ou_b", "om_bot_2", "like", ask_id)

    weights = feedback_weights()
    assert weights[("spec", "good-spec")] > 0


def test_old_feedback_decays(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_old", [{"type": "spec", "ref": "ancient"}])
    _add_reaction("ou_c", "om_old", "like", ask_id, days_ago=60)

    ask_id2 = _add_ask("om_new", [{"type": "spec", "ref": "fresh"}])
    _add_reaction("ou_c", "om_new", "like", ask_id2, days_ago=1)

    weights = feedback_weights()
    assert weights[("spec", "ancient")] < weights[("spec", "fresh")]


def test_reaction_emoji_thumbsdown_negative(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_e", [{"type": "raw", "ref": "777"}])
    _add_reaction("ou_d", "om_e", "reaction:thumbsdown", ask_id)

    weights = feedback_weights()
    assert weights[("raw", "777")] < 0


def test_unknown_emoji_neutral(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_u", [{"type": "raw", "ref": "888"}])
    _add_reaction("ou_e", "om_u", "reaction:party", ask_id)

    weights = feedback_weights()
    assert weights.get(("raw", "888"), 0.0) == 0.0


def test_cancel_like_does_not_contribute(db, settings):
    from helper.ask.feedback_signal import feedback_weights

    ask_id = _add_ask("om_c", [{"type": "spec", "ref": "neutral"}])
    _add_reaction("ou_f", "om_c", "cancel_like", ask_id)

    weights = feedback_weights()
    assert weights.get(("spec", "neutral"), 0.0) == 0.0
