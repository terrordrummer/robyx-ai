"""Spec 006 US3 — continuous-task lifecycle contract tests.

Covers stop / resume / complete / delete semantics per
``contracts/lifecycle-ops.md`` including golden error messages on
misuse (FR-016, FR-017).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lifecycle_macros import (
    DispatchContext,
    MacroInvocation,
    handle_lifecycle_macros,
)


def _state(name: str, status: str = "running") -> dict:
    import uuid
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "status": status,
        "parent_workspace": "ops",
        "workspace_thread_id": 42,
        "dedicated_thread_id": 9999,
        "branch": "continuous/%s" % name,
        "work_dir": "/tmp",
        "updated_at": "",
        "history": [],
        "total_steps_completed": 0,
        "program": {"objective": "x"},
    }


def _entry(name: str, status: str = "pending") -> dict:
    return {
        "name": name,
        "type": "continuous",
        "agent_file": "agents/%s.md" % name,
        "status": status,
        "thread_id": "9999",
        "chat_id": -100,
        "scheduled_at": "2026-04-22T00:00:00+00:00",
    }


def _ctx(entries: list[dict], state_map: dict) -> DispatchContext:
    return DispatchContext(
        chat_id=-100,
        thread_id=42,
        queue_reader=lambda: list(entries),
        state_reader=lambda name: state_map.get(name),
    )


def _platform():
    p = AsyncMock()
    p.archive_topic = AsyncMock(return_value=True)
    p.edit_topic_title = AsyncMock(return_value=True)
    p.close_topic = AsyncMock(return_value=True)
    return p


def _manager():
    m = MagicMock()
    m.remove_agent = MagicMock()
    return m


# ── Stop ────────────────────────────────────────────────────────────────


def test_stop_preserves_state_and_topic(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    cont.save_state(
        cont.state_file_path("t"), _state("t", "running"),
    )
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","thread_id":"9999"}]')
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    entries = [_entry("t")]
    state_map = {"t": _state("t", "running")}
    ctx = _ctx(entries, state_map)

    asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("stop_task", "t", (0, 0))], ctx,
    ))
    new_state = cont.load_state(cont.state_file_path("t"))
    assert new_state["status"] == "stopped"
    # Dedicated topic reference preserved.
    assert new_state["dedicated_thread_id"] == 9999


def test_stop_is_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    cont.save_state(cont.state_file_path("t"), _state("t", "running"))
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","thread_id":"9999"}]')
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    # Stop → Resume round-trip.
    entries = [_entry("t")]
    ctx = _ctx(entries, {"t": _state("t", "running")})
    asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("stop_task", "t", (0, 0))], ctx,
    ))
    stopped = cont.load_state(cont.state_file_path("t"))
    ctx2 = _ctx(entries, {"t": stopped})
    asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("resume_task", "t", (0, 0))], ctx2,
    ))
    resumed = cont.load_state(cont.state_file_path("t"))
    assert resumed["status"] == "pending"


# ── Complete ────────────────────────────────────────────────────────────


def test_complete_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    cont.save_state(cont.state_file_path("t"), _state("t", "running"))
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","thread_id":"9999"}]')
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    entries = [_entry("t")]
    ctx = _ctx(entries, {"t": _state("t", "running")})
    subs = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("complete_task", "t", (0, 0))], ctx,
    ))
    body = list(subs.values())[0]
    assert "completato" in body.lower()
    completed = cont.load_state(cont.state_file_path("t"))
    assert completed["status"] == "completed"


# ── Delete ──────────────────────────────────────────────────────────────


def test_delete_archives_topic_and_frees_name(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr("config.AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    agent_file = tmp_path / "agents" / "t.md"
    agent_file.write_text("# Agent")

    import continuous as cont

    cont.save_state(cont.state_file_path("t"), _state("t", "running"))
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","thread_id":"9999"}]')
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    platform = _platform()
    manager = _manager()
    entries = [_entry("t")]
    ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        platform=platform,
        manager=manager,
        queue_reader=lambda: list(entries),
        state_reader=lambda n: _state("t", "running") if n == "t" else None,
    )

    subs = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("delete_task", "t", (0, 0))], ctx,
    ))
    body = list(subs.values())[0]
    assert "eliminato" in body.lower()

    # Archive called on dedicated topic.
    platform.archive_topic.assert_awaited_once_with(9999, "t")
    # Agent file removed.
    assert not agent_file.exists()
    # Manager.remove_agent called.
    manager.remove_agent.assert_called_once_with("t")
    # State marked deleted with archived_at.
    final = cont.load_state(cont.state_file_path("t"))
    assert final["status"] == "deleted"
    assert final["archived_at"] is not None


# ── Golden error messages (FR-016, FR-017) ─────────────────────────────


def test_resume_not_found_message_points_to_get_events(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    queue = tmp_path / "queue.json"
    queue.write_text("[]")
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    ctx = DispatchContext(
        chat_id=-100, thread_id=42,
        queue_reader=lambda: [],
        state_reader=lambda _: None,
    )
    subs = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("resume_task", "zeus-rd-172", (0, 0))], ctx,
    ))
    body = list(subs.values())[0]
    assert "non trovato" in body.lower()
    # Golden: mentions [GET_EVENTS] and archived topic.
    assert "GET_EVENTS" in body
    assert "Archived" in body
    assert "CONTINUOUS" in body  # recreation hint


def test_delete_not_found_message_is_same_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    queue = tmp_path / "queue.json"
    queue.write_text("[]")
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    ctx = DispatchContext(
        chat_id=-100, thread_id=42,
        queue_reader=lambda: [],
        state_reader=lambda _: None,
    )
    subs = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("delete_task", "vanished", (0, 0))], ctx,
    ))
    body = list(subs.values())[0]
    assert "GET_EVENTS" in body
    assert "Archived" in body


def test_name_taken_message_tells_user_to_delete_first():
    """The i18n string used for name_taken must include DELETE_TASK
    as the concrete next action (FR-016 golden error)."""
    from i18n import STRINGS
    msg = STRINGS["continuous_task_error_name_taken"] % (
        "zeus-research", "zeus-research",
    )
    assert "already registered" in msg
    assert "DELETE_TASK" in msg
    # Tells user the name is what they need to free.
    assert "zeus-research" in msg
