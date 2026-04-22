"""Spec 006 US2 — dedicated-topic creation acceptance tests.

Verifies that `create_continuous_workspace` opens a dedicated topic at
creation time, stores `dedicated_thread_id`, points the queue entry at
it, and journals a `created` event.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import topics


@pytest.fixture
def agent_manager(monkeypatch, tmp_path):
    import agents as agents_mod
    # Fresh manager per test.
    mgr = agents_mod.AgentManager()
    return mgr


@pytest.fixture
def program():
    return {
        "objective": "Test dedicated-topic creation flow.",
        "success_criteria": ["Topic exists"],
        "constraints": [],
        "checkpoint_policy": "on-demand",
        "context": "",
        "first_step": {"number": 1, "description": "begin"},
    }


@pytest.fixture
def mock_platform_spec006():
    """Platform with the spec-006 ABC extensions wired."""
    plat = AsyncMock()
    plat.send_message = AsyncMock()
    plat.send_to_channel = AsyncMock(return_value=True)
    plat.create_channel = AsyncMock(return_value=7777)
    plat.edit_topic_title = AsyncMock(return_value=True)
    plat.pin_message = AsyncMock(return_value=True)
    plat.unpin_message = AsyncMock(return_value=True)
    plat.is_owner = MagicMock(return_value=True)
    plat.is_main_thread = MagicMock(return_value=False)
    plat.rename_main_channel = AsyncMock(return_value=True)
    plat.close_channel = AsyncMock(return_value=True)
    plat.max_message_length = 4000
    plat.control_room_id = 1
    return plat


async def test_creates_channel_with_bracketed_name(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    result = await topics.create_continuous_workspace(
        name="Sample Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
    )
    assert result is not None
    mock_platform_spec006.create_channel.assert_awaited_once_with(
        "[Continuous] Sample Task",
    )


async def test_initial_state_marker_applied(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    await topics.create_continuous_workspace(
        name="Sample Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
    )
    # edit_topic_title was called with running-state marker.
    mock_platform_spec006.edit_topic_title.assert_awaited_once()
    args = mock_platform_spec006.edit_topic_title.call_args.args
    assert args[0] == 7777
    assert "· ▶" in args[1]


async def test_queue_entry_thread_id_is_dedicated(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    await topics.create_continuous_workspace(
        name="Sample Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
    )
    queue_file = tmp_path / "data" / "queue.json"
    queue = json.loads(queue_file.read_text())
    entries = queue.get("entries") if isinstance(queue, dict) else queue
    continuous_entries = [e for e in entries if e.get("type") == "continuous"]
    assert len(continuous_entries) == 1
    # Queue points at the dedicated topic, not the parent.
    assert continuous_entries[0]["thread_id"] == "7777"


async def test_state_stores_dedicated_thread_id(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    await topics.create_continuous_workspace(
        name="Sample Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
    )
    state_file = tmp_path / "data" / "continuous" / "sample-task" / "state.json"
    state = json.loads(state_file.read_text())
    assert state["dedicated_thread_id"] == 7777
    assert state["workspace_thread_id"] == 42  # parent preserved


async def test_drain_timeout_override_persisted(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    await topics.create_continuous_workspace(
        name="LongTask",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
        drain_timeout_seconds=7200,
    )
    state_file = tmp_path / "data" / "continuous" / "longtask" / "state.json"
    state = json.loads(state_file.read_text())
    assert state["drain_timeout_seconds"] == 7200


async def test_created_event_journaled(
    tmp_path, agent_manager, mock_platform_spec006, program, monkeypatch,
):
    import events as events_mod

    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    await topics.create_continuous_workspace(
        name="Sample Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=mock_platform_spec006,
        parent_thread_id=42,
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="sample-task")
    created_events = [e for e in entries if e["event_type"] == "created"]
    assert len(created_events) == 1
    assert created_events[0]["payload"]["dedicated_thread_id"] == 7777


async def test_platform_failure_falls_back_to_parent_thread(
    tmp_path, agent_manager, program, monkeypatch,
):
    """If create_channel fails, the task still gets created and delivery
    falls back to the parent workspace thread.
    """
    plat = AsyncMock()
    plat.create_channel = AsyncMock(return_value=None)  # simulate failure
    plat.edit_topic_title = AsyncMock(return_value=False)
    plat.send_message = AsyncMock()
    plat.max_message_length = 4000
    plat.control_room_id = 1

    monkeypatch.setattr(
        "continuous.CONTINUOUS_DIR", tmp_path / "data" / "continuous",
    )
    work_dir = tmp_path / "project"
    work_dir.mkdir()

    result = await topics.create_continuous_workspace(
        name="Fallback Task",
        program=program,
        work_dir=str(work_dir),
        parent_workspace="ops",
        model="powerful",
        manager=agent_manager,
        platform=plat,
        parent_thread_id=42,
    )
    assert result is not None
    assert result["dedicated_thread_id"] is None
    assert result["thread_id"] == 42  # fallback to parent


# ── State-marker helper ─────────────────────────────────────────────────


async def test_update_topic_state_marker_computes_title(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    """`update_topic_state_marker` composes the new title from the
    current state and calls `edit_topic_title`.
    """
    import continuous as cont

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="marker-test", parent_workspace="p",
        program={"objective": "x"},
        thread_id=1, branch="b", work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 5555
    state["status"] = "awaiting_input"

    ok = await cont.update_topic_state_marker(
        state, mock_platform_spec006, display_name="Marker Test",
    )
    assert ok is True
    mock_platform_spec006.edit_topic_title.assert_awaited_once()
    args = mock_platform_spec006.edit_topic_title.call_args.args
    assert args[0] == 5555
    assert "· ⏸" in args[1]
    assert "Marker Test" in args[1]


async def test_update_topic_state_marker_no_dedicated_returns_false(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    import continuous as cont

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="nomark", parent_workspace="p", program={},
        thread_id=1, branch="b", work_dir="/tmp",
    )
    state["dedicated_thread_id"] = None
    ok = await cont.update_topic_state_marker(state, mock_platform_spec006)
    assert ok is False
    mock_platform_spec006.edit_topic_title.assert_not_awaited()


# ── Pin/unpin helpers ───────────────────────────────────────────────────


async def test_pin_awaiting_message_records_msg_id(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    import continuous as cont

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="pin-test", parent_workspace="p", program={},
        thread_id=1, branch="b", work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 3333

    ok = await cont.pin_awaiting_message(
        state, mock_platform_spec006, chat_id=-100, message_id=77,
    )
    assert ok is True
    assert state["awaiting_pinned_msg_id"] == 77


async def test_unpin_awaiting_message_clears_msg_id(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    import continuous as cont

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="unpin-test", parent_workspace="p", program={},
        thread_id=1, branch="b", work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 4444
    state["awaiting_pinned_msg_id"] = 99

    ok = await cont.unpin_awaiting_message(
        state, mock_platform_spec006, chat_id=-100,
    )
    assert ok is True
    assert state["awaiting_pinned_msg_id"] is None
