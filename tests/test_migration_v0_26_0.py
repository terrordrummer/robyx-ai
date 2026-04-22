"""Spec 006 — migration v0_26_0 tests.

Covers: idempotency gate, partial-failure safety, dedicated-topic
creation per existing task, journal seeding, awaiting-pin retroaction.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from migrations.base import MigrationContext
from migrations import v0_26_0


@pytest.fixture
def manager_stub():
    mgr = MagicMock()
    mgr.get = MagicMock(return_value=MagicMock(chat_id=-100))
    return mgr


@pytest.fixture
def platform_stub():
    plat = AsyncMock()
    # Allocate unique thread_ids per create_channel call.
    counter = {"n": 10_000}

    async def _create(name):
        counter["n"] += 1
        return counter["n"]

    plat.create_channel = AsyncMock(side_effect=_create)
    plat.edit_topic_title = AsyncMock(return_value=True)
    plat.pin_message = AsyncMock(return_value=True)
    plat.send_message = AsyncMock(return_value={"message_id": 555})
    plat.control_room_id = 1
    plat.max_message_length = 4000
    return plat


def _seed_task(
    data_dir: Path,
    name: str,
    status: str = "running",
    question: str | None = None,
) -> None:
    """Write a pre-v0.26 continuous-task state file."""
    task_dir = data_dir / "continuous" / name
    task_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "id": "x",
        "name": name,
        "status": status,
        "parent_workspace": "ops",
        "workspace_thread_id": 42,
        "branch": "continuous/%s" % name,
        "work_dir": "/tmp",
        "program": {"objective": "x"},
    }
    if question:
        state["awaiting_question"] = question
    (task_dir / "state.json").write_text(json.dumps(state))


async def test_migration_creates_dedicated_topics_per_task(
    tmp_path, manager_stub, platform_stub, monkeypatch,
):
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(tmp_path, "alpha")
    _seed_task(tmp_path, "beta")
    _seed_task(tmp_path, "gamma", status="stopped")

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=platform_stub,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx)

    # One create_channel per non-deleted task.
    assert platform_stub.create_channel.await_count == 3

    # Each state now has dedicated_thread_id + migrated_v0_26_0.
    for name in ("alpha", "beta", "gamma"):
        state = json.loads(
            (tmp_path / "continuous" / name / "state.json").read_text()
        )
        assert state["dedicated_thread_id"] is not None
        assert state["migrated_v0_26_0"] is not None


async def test_migration_is_idempotent(
    tmp_path, manager_stub, platform_stub, monkeypatch,
):
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(tmp_path, "alpha")

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=platform_stub,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx)
    first_id = json.loads(
        (tmp_path / "continuous" / "alpha" / "state.json").read_text()
    )["dedicated_thread_id"]

    # Second run: done marker present, but also per-task timestamp
    # prevents re-migration. create_channel must NOT be called again.
    platform_stub.create_channel.reset_mock()
    await v0_26_0.upgrade(ctx)

    platform_stub.create_channel.assert_not_awaited()
    second_id = json.loads(
        (tmp_path / "continuous" / "alpha" / "state.json").read_text()
    )["dedicated_thread_id"]
    assert first_id == second_id


async def test_migration_resumable_after_partial_failure(
    tmp_path, manager_stub, monkeypatch,
):
    """If the migration crashes halfway (e.g. rate-limit), re-running it
    completes the remaining tasks without re-migrating the done ones.
    """
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(tmp_path, "alpha")
    _seed_task(tmp_path, "beta")

    # First platform that fails on 'beta' (second call).
    call_count = {"n": 0}

    async def _create(name):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return 30001
        return None  # failure signals "beta" unresolved

    failing_plat = AsyncMock()
    failing_plat.create_channel = AsyncMock(side_effect=_create)
    failing_plat.edit_topic_title = AsyncMock(return_value=True)

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=failing_plat,
        manager=manager_stub,
    )
    with pytest.raises(RuntimeError):
        await v0_26_0.upgrade(ctx)

    # Alpha migrated; beta still pending.
    alpha_state = json.loads(
        (tmp_path / "continuous" / "alpha" / "state.json").read_text()
    )
    beta_state = json.loads(
        (tmp_path / "continuous" / "beta" / "state.json").read_text()
    )
    assert alpha_state["dedicated_thread_id"] == 30001
    assert beta_state.get("dedicated_thread_id") in (None, "")
    assert beta_state.get("migrated_v0_26_0") is None

    # Second run with a working platform completes beta only.
    counter2 = {"n": 40_000}

    async def _create_ok(name):
        counter2["n"] += 1
        return counter2["n"]

    good_plat = AsyncMock()
    good_plat.create_channel = AsyncMock(side_effect=_create_ok)
    good_plat.edit_topic_title = AsyncMock(return_value=True)

    ctx2 = MigrationContext(
        data_dir=tmp_path,
        platform=good_plat,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx2)

    good_plat.create_channel.assert_awaited_once()  # only beta
    beta_state_v2 = json.loads(
        (tmp_path / "continuous" / "beta" / "state.json").read_text()
    )
    assert beta_state_v2["dedicated_thread_id"] == 40_001


async def test_migration_seeds_journal_event(
    tmp_path, manager_stub, platform_stub, monkeypatch,
):
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(tmp_path, "alpha")

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=platform_stub,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx)

    # journal entry present in tmp_path/events.jsonl
    journal = tmp_path / "events.jsonl"
    assert journal.exists()
    lines = [
        json.loads(line)
        for line in journal.read_text().splitlines()
        if line.strip()
    ]
    migration_entries = [
        e for e in lines if e["event_type"] == "migration" and e["task_name"] == "alpha"
    ]
    assert len(migration_entries) == 1
    assert migration_entries[0]["payload"]["new_thread_id"] is not None


async def test_migration_skips_deleted_tasks(
    tmp_path, manager_stub, platform_stub, monkeypatch,
):
    """Tasks already in `deleted` state don't need a new dedicated topic."""
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(tmp_path, "zombie", status="deleted")

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=platform_stub,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx)

    platform_stub.create_channel.assert_not_awaited()
    state = json.loads(
        (tmp_path / "continuous" / "zombie" / "state.json").read_text()
    )
    # Still marked migrated (no-op) so a future run skips.
    assert state["migrated_v0_26_0"] is not None


async def test_migration_handles_awaiting_input_task(
    tmp_path, manager_stub, platform_stub, monkeypatch,
):
    """Tasks in awaiting_input get a retroactive pinned message."""
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.DATA_DIR", tmp_path)

    _seed_task(
        tmp_path, "needs-input",
        status="awaiting-input", question="pick a direction",
    )

    ctx = MigrationContext(
        data_dir=tmp_path,
        platform=platform_stub,
        manager=manager_stub,
    )
    await v0_26_0.upgrade(ctx)

    # send_message was invoked to post the retroactive pin.
    assert platform_stub.send_message.await_count >= 1
    # pin_message invoked with the returned msg_id.
    assert platform_stub.pin_message.await_count >= 1
