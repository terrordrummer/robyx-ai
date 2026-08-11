"""Scheduler/delivery wiring for spec-006 user-visible state transitions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


class _RefPlatform:
    max_message_length = 4000
    control_room_id = 0

    def __init__(self):
        self._send_ref = AsyncMock(return_value={"message_id": 91})
        self.send_to_channel = AsyncMock(return_value=True)
        self.pin_message = AsyncMock(return_value=True)
        self.unpin_message = AsyncMock(return_value=True)
        self.edit_topic_title = AsyncMock(return_value=True)

    async def send_to_channel_with_ref(self, channel_id, text, parse_mode=None):
        return await self._send_ref(channel_id, text, parse_mode=parse_mode)


@pytest.mark.asyncio
async def test_awaiting_delivery_is_pinned_and_marker_persisted(tmp_path, monkeypatch):
    import continuous
    import events
    from scheduled_delivery import deliver_task_output

    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(events, "append", MagicMock())
    state = continuous.create_continuous_task(
        name="awaiting",
        parent_workspace="ops",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/awaiting",
        work_dir=str(tmp_path),
    )
    continuous.set_awaiting_input(state, "Which option?")
    state["dedicated_thread_id"] = 700
    continuous.save_state(continuous.state_file_path("awaiting"), state)
    output = tmp_path / "output.log"
    output.write_text("Which option?")
    backend = MagicMock()
    backend.parse_response.return_value = "Which option?"
    platform = _RefPlatform()

    assert await deliver_task_output(
        {"name": "awaiting", "type": "continuous", "thread_id": "42"},
        output,
        platform,
        backend,
        0,
        MagicMock(),
    ) is True

    fresh = continuous.load_state(continuous.state_file_path("awaiting"))
    assert fresh["awaiting_pinned_msg_id"] == 91
    assert fresh["topic_marker_status"] == "awaiting_input"
    platform.pin_message.assert_awaited_once()
    assert platform.pin_message.await_args.kwargs["thread_id"] == 700
    platform.edit_topic_title.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_unpins_when_task_leaves_awaiting(tmp_path, monkeypatch):
    import continuous
    import scheduler

    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    state = continuous.create_continuous_task(
        name="resumed",
        parent_workspace="ops",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/resumed",
        work_dir=str(tmp_path),
    )
    state["status"] = "pending"
    state["dedicated_thread_id"] = 701
    state["awaiting_pinned_msg_id"] = 92
    state["topic_marker_status"] = "awaiting_input"
    continuous.save_state(continuous.state_file_path("resumed"), state)
    platform = _RefPlatform()

    assert await scheduler._sync_continuous_topic_marker(
        state,
        "resumed",
        platform,
    ) is True
    fresh = continuous.load_state(continuous.state_file_path("resumed"))
    assert fresh["awaiting_pinned_msg_id"] is None
    assert fresh["topic_marker_status"] == "pending"
    platform.unpin_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_recovery_updates_marker_and_journal(tmp_path, monkeypatch):
    import continuous
    import events
    import scheduler

    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(events, "append", MagicMock())
    state = continuous.create_continuous_task(
        name="rate-task",
        parent_workspace="ops",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/rate-task",
        work_dir=str(tmp_path),
    )
    state["status"] = "rate_limited"
    state["rate_limited_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state["next_step"] = None
    state["dedicated_thread_id"] = 702
    state["topic_marker_status"] = "rate_limited"
    continuous.save_state(continuous.state_file_path("rate-task"), state)
    (tmp_path / "queue.json").write_text(json.dumps([{
        "name": "rate-task",
        "type": "continuous",
        "status": "pending",
        "state_file": str(continuous.state_file_path("rate-task")),
        "thread_id": "702",
    }]))
    platform = _RefPlatform()

    dispatched, errors = await scheduler._handle_continuous_entries(
        MagicMock(),
        platform,
    )
    assert dispatched == [] and errors == []
    fresh = continuous.load_state(continuous.state_file_path("rate-task"))
    assert fresh["status"] == "pending"
    assert fresh["topic_marker_status"] == "pending"
    assert any(
        call.kwargs.get("event_type") == "rate_limit_recovered"
        for call in events.append.call_args_list
    )


@pytest.mark.asyncio
async def test_orphan_threshold_posts_one_incident_and_error_marker(
    tmp_path,
    monkeypatch,
):
    import config
    import continuous
    import events
    import scheduler

    monkeypatch.setattr(config, "ORPHAN_INCIDENT_THRESHOLD", 1)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(events, "append", MagicMock())
    state = continuous.create_continuous_task(
        name="orphaned",
        parent_workspace="ops",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/orphaned",
        work_dir=str(tmp_path),
    )
    state["status"] = "running"
    state["dedicated_thread_id"] = 703
    payload = scheduler._handle_continuous_orphan(state, "orphaned")
    assert payload is not None
    continuous.save_state(continuous.state_file_path("orphaned"), state)
    platform = _RefPlatform()

    await scheduler._sync_continuous_topic_marker(
        state,
        "orphaned",
        platform,
        actionable_event="task_death",
    )
    assert await scheduler._deliver_orphan_incident(
        state,
        "orphaned",
        payload,
        platform,
    ) is True

    assert state["status"] == "error"
    platform.edit_topic_title.assert_awaited_once()
    platform.send_to_channel.assert_awaited_once()
