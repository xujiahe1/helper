"""Pytest fixtures — tmp DB + stub LLM/Wave/retrieve/bundle。

Athenai / Wave / KM 全部 stub,不打外部网络。每个测试用 tmp sqlite,settings 走
环境变量覆盖,清 lru_cache。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest


def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """放在 helper.* 任何 import 之前,确保 Settings() 能正常初始化。"""
    monkeypatch.setenv("ATHENAI_API_KEY", "test-fake-key")
    monkeypatch.setenv("HELPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HELPER_SPEC_GIT_DIR", str(tmp_path / "spec_repo"))
    monkeypatch.setenv("HELPER_ADMIN_SK", "test-sk")
    monkeypatch.setenv("HELPER_OWNER_DOMAIN", "owner")
    monkeypatch.setenv("WAVE_APP_ID", "")
    monkeypatch.setenv("WAVE_APP_SECRET", "")
    monkeypatch.setenv("WAVE_CALLBACK_AES_KEY", "")
    monkeypatch.setenv("WAVE_CALLBACK_SIGN_TOKEN", "")


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_env(monkeypatch, tmp_path)
    from helper.config import get_settings
    from helper.llm import reset_routing_cache

    get_settings.cache_clear()
    reset_routing_cache()
    s = get_settings()
    yield s
    get_settings.cache_clear()
    reset_routing_cache()


@pytest.fixture
def db(settings, tmp_path: Path):
    """每个测试一个独立 sqlite。返 engine。"""
    from helper.storage.db import init_engine

    db_path = tmp_path / "helper.sqlite"
    engine = init_engine(db_path)
    yield engine
    engine.dispose()


@pytest.fixture
def llm_stub(monkeypatch: pytest.MonkeyPatch):
    """注册 task_name → str|callable 的 stub map,patch 到所有 import 现场。

    用法:
        llm_stub.set("l1_structure", '[{"type":"decision",...}]')
        llm_stub.set("conflict_judge", lambda system, user, **kw: '{"verdict":"contradicts",...}')
    """

    class _LLMStub:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}
            self.calls: list[tuple[str, str, str]] = []  # (task, system, user)

        def set(self, task: str, handler: Any) -> None:
            self.handlers[task] = handler

        def __call__(self, task: str, *, system: str = "", user: str = "", **kwargs: Any) -> str:
            self.calls.append((task, system, user))
            h = self.handlers.get(task)
            if h is None:
                raise AssertionError(f"llm_stub: task {task!r} not registered")
            if callable(h):
                return h(system=system, user=user, **kwargs)
            return str(h)

    stub = _LLMStub()
    # patch 所有现场的 `run`(`from helper.llm import run` 已 bound)
    sites = [
        "helper.llm.router.run",
        "helper.llm.run",
        "helper.ingest.l1_structure.run",
        "helper.ingest.prefilter.run",
        "helper.inquiry.engine.run",
        "helper.conflict.detector.run",
        "helper.ask.runtime.run",
        "helper.scheduler.parser.run",
        "helper.specgen.draft.run",
        "helper.batch.ingest.run",
        "helper.im.intent.run",
        "helper.eval.replay.run",
        "helper.memory.extract.run",
        "helper.acl.tagger.run",
    ]
    for site in sites:
        try:
            monkeypatch.setattr(site, stub)
        except (ModuleNotFoundError, AttributeError):
            pass
    return stub


@pytest.fixture
def wave_send_log(monkeypatch: pytest.MonkeyPatch):
    """收集所有 wave_client.send_message / reply_message 调用,断言用。"""
    sent: list[dict] = []

    def _fake_send(receiver_id: str, **kwargs: Any) -> dict:
        sent.append({"receiver_id": receiver_id, **kwargs})
        return {"data": {"message_id": "om_fake"}, "retcode": 0}

    def _fake_reply(*, msg_id: str, **kwargs: Any) -> dict:
        sent.append({"reply_to": msg_id, **kwargs})
        return {"data": {"message_id": "om_fake_reply"}, "retcode": 0}

    monkeypatch.setattr("helper.im.wave_client.send_message", _fake_send)
    monkeypatch.setattr("helper.im.wave_client.reply_message", _fake_reply)
    monkeypatch.setattr("helper.inbox.weekly.wave_client.send_message", _fake_send)
    return sent


@pytest.fixture
def retrieve_stub(monkeypatch: pytest.MonkeyPatch):
    """注册 question → list[Hit] 的 stub。默认返空。"""
    from helper.ask.retrieve import Hit

    hits_for_query: list[Hit] = []

    def _fake_retrieve(question: str, *, top_k: int = 8, asker_domain: str = "") -> list[Hit]:
        return list(hits_for_query)[:top_k]

    monkeypatch.setattr("helper.ask.retrieve.retrieve_relevant", _fake_retrieve)
    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", _fake_retrieve)
    monkeypatch.setattr("helper.conflict.detector.retrieve_relevant", _fake_retrieve)

    class _Ctrl:
        def set(self, hits: list[Hit]) -> None:
            hits_for_query.clear()
            hits_for_query.extend(hits)

        @staticmethod
        def hit(type: str, ref: str, title: str = "", body: str = "", score: float = 1.0) -> Hit:
            return Hit(type=type, ref=ref, title=title, body=body, score=score)

    return _Ctrl()


@pytest.fixture
def stub_index_raw(monkeypatch: pytest.MonkeyPatch):
    """跳过向量索引(避免打 embed 接口)。"""

    def _noop(sess, raw_id: int) -> None:
        return None

    monkeypatch.setattr("helper.storage.vector.index_raw", _noop)


@pytest.fixture
def stub_bundle(monkeypatch: pytest.MonkeyPatch):
    """patch load_bundle 返空结构,避免依赖真实 git repo。"""

    def _empty_bundle() -> dict:
        return {
            "version": "test",
            "built_at": "2026-01-01T00:00:00Z",
            "entities": [],
            "relationships": [],
            "specs": [],
            "facts": [],
            "cases": [],
        }

    monkeypatch.setattr("helper.compiler.load_bundle", _empty_bundle)
    monkeypatch.setattr("helper.compiler.build.load_bundle", _empty_bundle)
    monkeypatch.setattr("helper.web.browser.load_bundle", _empty_bundle)
    monkeypatch.setattr("helper.compiler.current_bundle_version", lambda: "test")
    monkeypatch.setattr("helper.web.browser.current_bundle_version", lambda: "test")


@pytest.fixture
def app_client(db, settings, wave_send_log, stub_bundle):
    """FastAPI TestClient,带 admin sk header 预置。"""
    from fastapi.testclient import TestClient

    from helper.server import create_app

    app = create_app()
    client = TestClient(app)
    client.headers.update({"X-Helper-Admin-Key": "test-sk"})
    return client


# ---- 便捷工厂 ----

@pytest.fixture
def make_raw(db):
    """建一条 raw_inputs,返回其 id。"""
    from helper.storage import session
    from helper.storage.models import RawInput

    def _make(
        text: str = "测试输入",
        *,
        source_type: str = "cli",
        author_domain: str = "owner",
        chat_id: str = "",
        is_at_bot: bool = False,
    ) -> int:
        with session() as s:
            r = RawInput(
                source_type=source_type,
                content_text=text,
                author_domain=author_domain,
                chat_id=chat_id,
                is_at_bot=is_at_bot,
            )
            s.add(r)
            s.flush()
            return r.id

    return _make
