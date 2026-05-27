"""scheduler.tasks.dispatch + runner.ensure_owner_inbox_weekly."""

from __future__ import annotations

import asyncio


def test_dispatch_inbox_weekly_invokes_send_to(db, settings, monkeypatch):
    from helper.scheduler.tasks import dispatch

    called: list[tuple[str, str]] = []

    def _fake_send_to(receiver_id: str, *, receiver_id_type: str = "user_id") -> bool:
        called.append((receiver_id, receiver_id_type))
        return True

    monkeypatch.setattr("helper.inbox.send_to", _fake_send_to)

    snapshot = {
        "id": 1,
        "task_type": "inbox_weekly",
        "params_json": "{}",
        "receiver_id": "owner",
        "receiver_id_type": "user_id",
        "owner_user_id": "owner",
        "summary": "weekly",
    }
    asyncio.run(dispatch(snapshot))
    assert called == [("owner", "user_id")]


def test_dispatch_unknown_task_type_noop(db, settings):
    from helper.scheduler.tasks import dispatch

    asyncio.run(dispatch({"id": 99, "task_type": "__nope__"}))


def test_ensure_owner_inbox_weekly_idempotent(db, settings):
    from sqlalchemy import select

    from helper.scheduler.runner import ensure_owner_inbox_weekly
    from helper.storage import session
    from helper.storage.models import ScheduledTask

    ensure_owner_inbox_weekly()
    ensure_owner_inbox_weekly()  # 第二次应 noop

    with session() as s:
        rows = list(s.execute(
            select(ScheduledTask)
            .where(ScheduledTask.owner_user_id == "owner")
            .where(ScheduledTask.task_type == "inbox_weekly")
        ).scalars())
    assert len(rows) == 1
    assert rows[0].cron_expr == "0 9 * * 1"
    assert rows[0].enabled is True


def test_ensure_owner_inbox_weekly_no_owner(monkeypatch, db, tmp_path):
    """settings.helper_owner_domain 空 → 不建任务。"""
    monkeypatch.setenv("ATHENAI_API_KEY", "test-fake-key")
    monkeypatch.setenv("HELPER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HELPER_SPEC_GIT_DIR", str(tmp_path / "g"))
    monkeypatch.setenv("HELPER_OWNER_DOMAIN", "")

    from helper.config import get_settings
    get_settings.cache_clear()

    from sqlalchemy import select

    from helper.scheduler.runner import ensure_owner_inbox_weekly
    from helper.storage import session
    from helper.storage.models import ScheduledTask

    ensure_owner_inbox_weekly()
    with session() as s:
        rows = list(s.execute(
            select(ScheduledTask).where(ScheduledTask.task_type == "inbox_weekly")
        ).scalars())
    assert rows == []
    get_settings.cache_clear()
