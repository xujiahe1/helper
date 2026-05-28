"""wave_webhook 事件白名单分发回归。

设计:未被白名单收下的事件类型(如 im.chat.bot.entered_v1 / im.chat.members.added_v1
/ auth.* / docs.*),不应该再落 raw_inputs;白名单内的:
  - feedback / reaction → 落 ReactionLog
  - direct.sent_v2 / group.sent_v2 → 落 raw_inputs
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _encrypt_payload(plaintext: str, secret_key: str) -> str:
    key = secret_key.encode("utf-8")
    iv = key[:16]
    pt = plaintext.encode("utf-8")
    pad = 16 - (len(pt) % 16)
    pt += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ct = enc.update(pt) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _signature(ts: str, nonce: str, plaintext: str, sign_token: str) -> str:
    msg = ts.encode() + nonce.encode() + plaintext.encode() + sign_token.encode()
    return hashlib.sha256(msg).hexdigest()


@pytest.fixture
def webhook_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ATHENAI_API_KEY", "test-fake-key")
    monkeypatch.setenv("HELPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HELPER_SPEC_GIT_DIR", str(tmp_path / "spec_repo"))
    monkeypatch.setenv("HELPER_ADMIN_SK", "test-sk")
    monkeypatch.setenv("WAVE_APP_ID", "cli_test")
    monkeypatch.setenv("WAVE_APP_SECRET", "secret")
    aes_key = "0123456789abcdef" * 2  # 32 bytes for AES-256
    sign_token = "tok"
    monkeypatch.setenv("WAVE_CALLBACK_AES_KEY", aes_key)
    monkeypatch.setenv("WAVE_CALLBACK_SIGN_TOKEN", sign_token)

    from helper.config import get_settings
    from helper.llm import reset_routing_cache
    from helper.storage.db import init_engine

    get_settings.cache_clear()
    reset_routing_cache()
    init_engine(tmp_path / "helper.sqlite")

    # 屏蔽所有后台调度,避免测试里飞起来打外部
    monkeypatch.setattr("helper.im.wave_webhook.schedule_ask_reply", lambda **kw: None)
    monkeypatch.setattr("helper.im.wave_webhook.schedule_post_message", lambda *a, **kw: None)
    monkeypatch.setattr("helper.im.wave_webhook.schedule_l1", lambda *a, **kw: None)

    from helper.server import create_app
    app = create_app()
    client = TestClient(app)

    def _post(payload: dict) -> Any:
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encrypted = _encrypt_payload(plaintext, aes_key)
        ts = "1700000000"
        nonce = "n1"
        sig = _signature(ts, nonce, plaintext, sign_token)
        return client.post(
            "/callback",
            content=json.dumps({"encrypt": encrypted}),
            headers={
                "hoyowave-open-signature": sig,
                "hoyowave-open-timestamp": ts,
                "hoyowave-open-nonce": nonce,
                "content-type": "application/json",
            },
        )

    yield _post
    get_settings.cache_clear()


def _count_raw(suffix: str = "") -> int:
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        rows = s.query(RawInput).all()
        if suffix:
            return sum(1 for r in rows if r.source_type and suffix in r.source_type)
        return len(rows)


def test_unknown_event_does_not_pollute_raw_inputs(webhook_client):
    """im.chat.bot.entered_v1 不在白名单 → drop,不应进 raw_inputs。"""
    payload = {
        "schema": "1.0",
        "header": {"event_type": "im.chat.bot.entered_v1", "event_id": "evt-bot-entered-1"},
        "event": {"chat_id": "oc_x"},
    }
    resp = webhook_client(payload)
    assert resp.status_code == 200
    assert _count_raw() == 0


def test_message_event_still_lands_in_raw_inputs(webhook_client):
    payload = {
        "schema": "1.0",
        "header": {"event_type": "im.msg.direct.sent_v2", "event_id": "evt-msg-1"},
        "event": {
            "sender": {"id": "ou_a", "id_type": "union_id", "user_id": "alice"},
            "receiver": {"id": "cli_test", "id_type": "app_id"},
            "message": {
                "msg_id": "om_1",
                "msg_type": "text",
                "content": json.dumps({"text": "hello"}),
            },
        },
    }
    resp = webhook_client(payload)
    assert resp.status_code == 200
    assert _count_raw() == 1


def test_reaction_event_lands_in_reaction_log_not_raw(webhook_client):
    """reaction 事件应进 ReactionLog,不进 raw_inputs。"""
    from helper.storage import session
    from helper.storage.models import ReactionLog

    payload = {
        "schema": "1.0",
        "header": {"event_type": "im.msg.reaction.created_v1", "event_id": "evt-rx-1"},
        "event": {
            "msg_id": "om_bot_1",
            "operator": {"id": "ou_user", "id_type": "union_id", "user_id": "bob"},
            "reaction_type": {"emoji_type": "thumbsup"},
        },
    }
    resp = webhook_client(payload)
    assert resp.status_code == 200
    assert _count_raw() == 0
    with session() as s:
        row = s.get(ReactionLog, ("ou_user", "om_bot_1"))
    assert row is not None
    assert row.action_type == "reaction:thumbsup"
