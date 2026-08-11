"""Offline executable harness for spec-006 quickstart sections 1–9.

The long-lived timing and adapter details remain covered by their focused
suites. This file composes the central transaction across those sections:
create an isolated task, exercise lifecycle/name reservation, delete and
recreate it, then recover a lost dedicated topic without any network access.
Quickstart section 10 is deliberately the documented manual seven-day run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _program() -> dict:
    return {
        "objective": "Exercise the spec-006 offline quickstart.",
        "success_criteria": ["lifecycle and recovery remain isolated"],
        "constraints": ["no network"],
        "checkpoint_policy": "on-demand",
        "context": "quickstart harness",
        "first_step": {"number": 1, "description": "begin"},
    }


@pytest.mark.asyncio
async def test_quickstart_create_lifecycle_recreate_and_recover(
    tmp_path,
    monkeypatch,
):
    import agents
    import continuous
    import events
    import scheduler
    import topics
    from lifecycle_macros import (
        DispatchContext,
        MacroInvocation,
        handle_lifecycle_macros,
    )
    from task_scope import TaskScope
    from topic_recovery import recover_unreachable_topic

    data_dir = tmp_path / "data"
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", data_dir / "continuous")
    monkeypatch.setattr(scheduler, "DATA_DIR", data_dir)
    monkeypatch.setattr(scheduler, "QUEUE_FILE", data_dir / "queue.json")
    monkeypatch.setattr(topics, "DATA_DIR", data_dir)
    monkeypatch.setattr(topics, "AGENTS_DIR", data_dir / "agents")
    monkeypatch.setattr("config.AGENTS_DIR", data_dir / "agents")
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    manager = agents.AgentManager()
    scope = TaskScope("telegram", "-1001", 42)

    platform = MagicMock()
    platform.create_channel = AsyncMock(side_effect=[7001, 7002, 7003])
    platform.edit_topic_title = AsyncMock(return_value=True)
    platform.send_to_channel = AsyncMock(return_value=True)
    platform.send_message = AsyncMock(return_value={"message_id": 1})
    platform.pin_message = AsyncMock(return_value=True)
    platform.unpin_message = AsyncMock(return_value=True)
    platform.archive_topic = AsyncMock(return_value=True)
    platform.close_channel = AsyncMock(return_value=True)
    platform.control_room_id = 1

    # §1–§3: creation is isolated to a dedicated topic and carries its
    # canonical parent scope. No scheduler/HQ message is generated.
    first = await topics.create_continuous_workspace(
        name="Quickstart Task",
        program=_program(),
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=manager,
        platform=platform,
        parent_thread_id=42,
        workspace_scope=scope,
        drain_timeout_seconds=120,
    )
    assert first is not None and first["dedicated_thread_id"] == 7001
    state = continuous.load_state(
        continuous.state_file_path("quickstart-task"),
    )
    assert state["dedicated_thread_id"] == 7001
    assert state["workspace_scope"] == scope.to_dict()
    assert state["drain_timeout_seconds"] == 120
    assert scheduler.load_queue()[0]["thread_id"] == "7001"
    platform.send_message.assert_not_awaited()

    ctx = DispatchContext(
        chat_id=-1001,
        thread_id=42,
        task_scope=scope,
        platform=platform,
        manager=manager,
    )

    # §4: stopped tasks reserve the name, resume is reversible, delete frees
    # it and preserves the archived topic.
    stopped = await handle_lifecycle_macros(
        [MacroInvocation("stop_task", "quickstart-task", (0, 0))],
        ctx,
    )
    assert "fermato" in next(iter(stopped.values())).lower()
    assert continuous.load_state(
        continuous.state_file_path("quickstart-task"),
    )["status"] == "stopped"
    with pytest.raises(ValueError, match="already in use|Name taken"):
        await topics.create_continuous_workspace(
            name="Quickstart Task",
            program=_program(),
            work_dir=str(work_dir),
            parent_workspace="ops",
            model="powerful",
            manager=manager,
            platform=platform,
            parent_thread_id=42,
            workspace_scope=scope,
        )

    resumed = await handle_lifecycle_macros(
        [MacroInvocation("resume_task", "quickstart-task", (0, 0))],
        ctx,
    )
    assert "ripreso" in next(iter(resumed.values())).lower()
    deleted = await handle_lifecycle_macros(
        [MacroInvocation("delete_task", "quickstart-task", (0, 0))],
        ctx,
    )
    assert "eliminato" in next(iter(deleted.values())).lower()
    assert continuous.load_state(
        continuous.state_file_path("quickstart-task"),
    )["status"] == "deleted"

    second = await topics.create_continuous_workspace(
        name="Quickstart Task",
        program=_program(),
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=manager,
        platform=platform,
        parent_thread_id=42,
        workspace_scope=scope,
    )
    assert second is not None and second["dedicated_thread_id"] == 7002

    # §5–§9: a lost topic is recreated, queue/state are repointed together,
    # pending output is replayed, and the ordinary recovery stays silent in HQ.
    platform.send_to_channel.reset_mock()
    recovered = await recover_unreachable_topic(
        "quickstart-task",
        platform,
        reason="TOPIC_ID_INVALID",
        pending_delivery="pending quickstart result",
    )
    assert recovered.recreated_thread_id == 7003
    assert recovered.pending_delivered is True
    fresh = continuous.load_state(
        continuous.state_file_path("quickstart-task"),
    )
    assert fresh["dedicated_thread_id"] == 7003
    assert scheduler.load_queue()[0]["thread_id"] == "7003"
    platform.send_to_channel.assert_awaited_once_with(
        7003,
        "pending quickstart result",
        parse_mode="Markdown",
    )
    platform.send_message.assert_not_awaited()

    recent = events.query(
        datetime.now(timezone.utc) - timedelta(minutes=1),
        task_name="quickstart-task",
    )
    event_types = {entry["event_type"] for entry in recent}
    assert {"created", "stopped", "resumed", "deleted", "topic_recreated"} <= event_types


def test_scheduler_grep_audit_keeps_continuous_hq_silent():
    """T070: pin the source audit so a direct continuous HQ push fails CI."""
    from pathlib import Path

    scheduler_path = Path(__file__).resolve().parents[1] / "bot" / "scheduler.py"
    source = scheduler_path.read_text(encoding="utf-8")
    reminder_start = source.index("async def _dispatch_reminders")
    continuous_start = source.index("async def _handle_continuous_entries")
    reminder_source = source[reminder_start:continuous_start]
    continuous_source = source[continuous_start:]

    # The sole direct platform.send_message belongs to user-addressed reminder
    # delivery. Continuous events use dedicated topics/recovery and journals.
    assert source.count("platform.send_message(") == 1
    assert "platform.send_message(" in reminder_source
    assert "platform.send_message(" not in continuous_source
    assert "append_log(\"%s -- DISPATCHED -- step" in continuous_source
    assert 'event_type="dispatched"' in continuous_source
    assert "failed to spawn" in continuous_source
    assert 'event_type="error"' in continuous_source
