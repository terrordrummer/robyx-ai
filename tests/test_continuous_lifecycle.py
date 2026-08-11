"""Spec 006 US3 — continuous-task lifecycle contract tests.

Covers stop / resume / complete / delete semantics per
``contracts/lifecycle-ops.md`` including golden error messages on
misuse (FR-016, FR-017).
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import lifecycle_macros as lifecycle_mod
from lifecycle_macros import (
    DispatchContext,
    MacroInvocation,
    handle_lifecycle_macros,
)
from task_scope import TaskScope


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


@pytest.mark.asyncio
async def test_unambiguous_legacy_scope_migrates_state_and_queue_together(
    tmp_path, monkeypatch,
):
    import config
    import continuous as cont
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(config, "PLATFORM", "telegram")
    monkeypatch.setattr(config, "CHAT_ID", -1001)
    state = _state("legacy", "running")
    cont.save_state(cont.state_file_path("legacy"), state)
    sched.add_task(_entry("legacy"), allow_legacy_unscoped=True)
    manager = SimpleNamespace(
        list_active=lambda: [SimpleNamespace(name="ops", thread_id=42)],
    )
    scope = TaskScope("telegram", "-1001", 42)
    ctx = DispatchContext(
        chat_id=-1001,
        thread_id=42,
        task_scope=scope,
        manager=manager,
    )

    rendered = await handle_lifecycle_macros(
        [MacroInvocation("list_tasks", None, (0, 0))], ctx,
    )

    assert "legacy" in list(rendered.values())[0]
    assert cont.load_state(cont.state_file_path("legacy"))["workspace_scope"] == (
        scope.to_dict()
    )
    assert sched.load_queue()[0]["workspace_scope"] == scope.to_dict()


@pytest.mark.asyncio
async def test_conflicting_queue_scope_is_hidden_and_never_overwritten(
    tmp_path, monkeypatch,
):
    import continuous as cont
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "queue.json")
    state = _state("conflict", "running")
    state["workspace_scope"] = TaskScope(
        "telegram", "-1001", 42,
    ).to_dict()
    cont.save_state(cont.state_file_path("conflict"), state)
    entry = _entry("conflict")
    entry["workspace_scope"] = TaskScope(
        "telegram", "-1002", 42,
    ).to_dict()
    sched.add_task(
        entry,
        scope=TaskScope("telegram", "-1002", 42),
        allow_legacy_unscoped=True,
    )
    ctx = DispatchContext(
        chat_id=-1001,
        thread_id=42,
        task_scope=TaskScope("telegram", "-1001", 42),
    )

    rendered = await handle_lifecycle_macros(
        [MacroInvocation("list_tasks", None, (0, 0))], ctx,
    )

    assert "Nessun task" in list(rendered.values())[0]
    assert sched.load_queue()[0]["workspace_scope"]["chat_id"] == "-1002"


# ── Stop ────────────────────────────────────────────────────────────────


def test_stop_preserves_state_and_topic(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    cont.save_state(
        cont.state_file_path("t"), _state("t", "running"),
    )
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
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

    state_snapshot = _state("t", "running")
    cont.save_state(cont.state_file_path("t"), state_snapshot)
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
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
    resumed_queue = json.loads(queue.read_text())[0]
    assert resumed_queue["status"] == "pending"
    assert "canceled_at" not in resumed_queue
    assert "canceled_reason" not in resumed_queue


# ── Complete ────────────────────────────────────────────────────────────


def test_complete_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    state_snapshot = _state("t", "running")
    cont.save_state(cont.state_file_path("t"), state_snapshot)
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
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

    state_snapshot = _state("t", "running")
    cont.save_state(cont.state_file_path("t"), state_snapshot)
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
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
        state_reader=lambda n: state_snapshot if n == "t" else None,
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


# ── Running-process termination and lifecycle races ─────────────────────


@pytest.mark.parametrize(
    ("macro", "expected_status"),
    [
        ("stop_task", "stopped"),
        ("complete_task", "completed"),
        ("delete_task", "deleted"),
    ],
)
async def test_terminal_lifecycle_op_reaps_process_and_wins_final_state(
    macro, expected_status, tmp_path, monkeypatch,
):
    """A running agent may write state while handling SIGTERM.

    The lifecycle operation must wait for that write/process exit and commit
    its authoritative state afterwards, so the task cannot be resurrected.
    """
    import continuous as cont
    import scheduler as sched

    name = "running-" + expected_status
    continuous_dir = tmp_path / "continuous"
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", continuous_dir)
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue)

    state = _state(name, "running")
    state["dedicated_thread_id"] = None
    state["drain_timeout_seconds"] = 2
    cont.save_state(cont.state_file_path(name), state)
    queue.write_text(json.dumps([_entry(name)]))

    # The child deliberately writes pending on SIGTERM, reproducing the old
    # step-agent behaviour that could overwrite a stop/complete/delete.
    child_script = tmp_path / "state_writer.py"
    child_script.write_text(
        """import json
import os
import signal
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])

def handle_term(_signum, _frame):
    state = json.loads(state_path.read_text())
    state[\"status\"] = \"pending\"
    tmp = state_path.with_suffix(\".child.tmp\")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_term)
ready_path.write_text(\"ready\")
time.sleep(60)
"""
    )
    ready = tmp_path / "child.ready"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(child_script),
        str(cont.state_file_path(name)),
        str(ready),
    )
    # Reap concurrently, mirroring the production delivery watcher.
    reaper = asyncio.create_task(proc.wait())
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()

    lock_file = tmp_path / name / "lock"
    lock_file.parent.mkdir(parents=True)
    sched._write_lock_file(lock_file, proc.pid)

    ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        queue_reader=lambda: json.loads(queue.read_text()),
        state_reader=lambda task_name: cont.load_state(
            cont.state_file_path(task_name)
        ),
    )
    substitutions = await handle_lifecycle_macros(
        [MacroInvocation(macro, name, (0, 0))], ctx,
    )
    assert "Errore nell'elaborare" not in list(substitutions.values())[0]

    await asyncio.wait_for(reaper, timeout=2)
    assert proc.returncode is not None
    assert not lock_file.exists()
    assert cont.load_state(cont.state_file_path(name))["status"] == expected_status
    assert json.loads(queue.read_text())[0]["status"] == "canceled"


async def test_dispatch_spawn_losing_lifecycle_race_is_terminated(
    tmp_path, monkeypatch,
):
    """A stop during create_subprocess_exec must not be overwritten by running."""
    import continuous as cont
    import scheduler as sched

    name = "spawn-race"
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "bot.log")
    queue = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue)

    cont.create_continuous_task(
        name=name,
        parent_workspace="ops",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/spawn-race",
        work_dir=str(tmp_path),
    )
    queue.write_text(json.dumps([_entry(name)]))

    fake_proc = MagicMock()
    fake_proc.pid = 54321
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=-15)

    async def spawn_then_stop(**_kwargs):
        sched.cancel_task_by_name(name, reason="concurrent stop")
        latest = cont.load_state(cont.state_file_path(name))
        cont.pause_task(latest)
        cont.save_state(cont.state_file_path(name), latest)
        return fake_proc

    monkeypatch.setattr(sched, "_spawn_ai_subprocess", spawn_then_stop)

    backend = MagicMock()
    backend.key = "claude"
    backend.build_spawn_command.return_value = ["unused"]
    backend.spawn_stdin_payload.return_value = None

    dispatched, errors = await sched._handle_continuous_entries(backend)

    assert dispatched == []
    assert errors == []
    fake_proc.terminate.assert_called_once_with()
    fake_proc.wait.assert_awaited_once_with()
    assert cont.load_state(cont.state_file_path(name))["status"] == "stopped"
    assert not (tmp_path / name / "lock").exists()


def test_delete_finds_completed_task_hidden_from_active_list(tmp_path, monkeypatch):
    """Delete applies to completed tasks even though LIST_TASKS hides them."""
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    state = _state("done", "completed")
    state["dedicated_thread_id"] = None
    cont.save_state(cont.state_file_path("done"), state)
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps([_entry("done")]))
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        queue_reader=lambda: json.loads(queue.read_text()),
        state_reader=lambda name: cont.load_state(cont.state_file_path(name)),
    )
    substitutions = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("delete_task", "done", (0, 0))], ctx,
    ))

    assert "eliminato" in list(substitutions.values())[0].lower()
    assert cont.load_state(cont.state_file_path("done"))["status"] == "deleted"


def test_delete_deleted_task_reports_idempotent_result(tmp_path, monkeypatch):
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
    import continuous as cont

    state = _state("gone", "deleted")
    state["archived_at"] = "2026-01-01T00:00:00+00:00"
    cont.save_state(cont.state_file_path("gone"), state)
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps([_entry("gone", status="canceled")]))
    monkeypatch.setattr("scheduler.QUEUE_FILE", queue)

    ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        queue_reader=lambda: json.loads(queue.read_text()),
        state_reader=lambda name: cont.load_state(cont.state_file_path(name)),
    )
    substitutions = asyncio.run(handle_lifecycle_macros(
        [MacroInvocation("delete_task", "gone", (0, 0))], ctx,
    ))

    body = list(substitutions.values())[0].lower()
    assert "già" in body
    assert "archiviato" in body


@pytest.mark.parametrize("competing_macro", ["stop_task", "complete_task"])
async def test_delete_wins_concurrent_terminal_transition(
    competing_macro, tmp_path, monkeypatch,
):
    """A stale stop/complete queued behind delete must not resurrect state."""
    import continuous as cont
    import scheduler as sched

    name = "delete-race-" + competing_macro
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue)

    state = _state(name, "running")
    cont.save_state(cont.state_file_path(name), state)
    queue.write_text(json.dumps([_entry(name)]))

    archive_entered = asyncio.Event()
    allow_archive = asyncio.Event()

    async def archive_topic(_thread_id, _display_name):
        archive_entered.set()
        await allow_archive.wait()
        return True

    platform = AsyncMock()
    platform.send_to_channel = AsyncMock(return_value=True)
    platform.archive_topic = AsyncMock(side_effect=archive_topic)
    platform.edit_topic_title = AsyncMock(return_value=True)

    ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        platform=platform,
        queue_reader=lambda: json.loads(queue.read_text()),
        state_reader=lambda task_name: cont.load_state(
            cont.state_file_path(task_name)
        ),
    )

    delete_call = asyncio.create_task(handle_lifecycle_macros(
        [MacroInvocation("delete_task", name, (0, 0))], ctx,
    ))
    await asyncio.wait_for(archive_entered.wait(), timeout=1)

    competing_call = asyncio.create_task(handle_lifecycle_macros(
        [MacroInvocation(competing_macro, name, (0, 0))], ctx,
    ))
    await asyncio.sleep(0)
    assert not competing_call.done()

    allow_archive.set()
    await asyncio.gather(delete_call, competing_call)

    assert cont.load_state(cont.state_file_path(name))["status"] == "deleted"
    assert name not in lifecycle_mod._lifecycle_locks


async def test_lifecycle_lock_is_per_task_and_registry_is_cleaned():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    same_name_entered = asyncio.Event()
    other_name_entered = asyncio.Event()

    async def first_holder():
        async with lifecycle_mod._lifecycle_task_lock("alpha"):
            first_entered.set()
            await release_first.wait()

    async def same_name_waiter():
        async with lifecycle_mod._lifecycle_task_lock("alpha"):
            same_name_entered.set()

    async def other_name_holder():
        async with lifecycle_mod._lifecycle_task_lock("beta"):
            other_name_entered.set()

    first = asyncio.create_task(first_holder())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    same = asyncio.create_task(same_name_waiter())
    other = asyncio.create_task(other_name_holder())

    await asyncio.wait_for(other_name_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not same_name_entered.is_set()

    release_first.set()
    await asyncio.gather(first, same, other)
    assert same_name_entered.is_set()
    assert "alpha" not in lifecycle_mod._lifecycle_locks
    assert "beta" not in lifecycle_mod._lifecycle_locks


async def test_delete_recreate_replaces_queue_tombstone_and_targets_new_generation(
    tmp_path, monkeypatch,
):
    """Delete frees a name without leaving an ambiguous lifecycle lookup."""
    import continuous as cont
    import scheduler as sched

    name = "phoenix"
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue)

    old_state = _state(name, "running")
    old_state["dedicated_thread_id"] = None
    cont.save_state(cont.state_file_path(name), old_state)
    sched.add_task({
        "name": name,
        "type": "continuous",
        "agent_file": "agents/%s.md" % name,
        "thread_id": "42",
        "state_file": str(cont.state_file_path(name)),
    }, allow_legacy_unscoped=True)

    dynamic_ctx = DispatchContext(
        chat_id=-100,
        thread_id=42,
        queue_reader=sched.load_queue,
        state_reader=lambda task_name: cont.load_state(
            cont.state_file_path(task_name)
        ),
    )
    deleted = await handle_lifecycle_macros(
        [MacroInvocation("delete_task", name, (0, 0))], dynamic_ctx,
    )
    assert "eliminato" in list(deleted.values())[0].lower()
    assert len(sched.load_queue()) == 1
    assert sched.load_queue()[0]["status"] == "canceled"

    new_state = cont.create_continuous_task(
        name=name,
        parent_workspace="ops",
        program={"objective": "new generation"},
        thread_id=42,
        branch="continuous/phoenix-v2",
        work_dir=str(tmp_path),
    )
    assert new_state["id"] != old_state["id"]
    sched.add_task({
        "name": name,
        "type": "continuous",
        "agent_file": "agents/%s.md" % name,
        "thread_id": "42",
        "state_file": str(cont.state_file_path(name)),
    }, allow_legacy_unscoped=True)

    recreated_queue = sched.load_queue()
    assert len(recreated_queue) == 1
    assert recreated_queue[0]["status"] == "pending"

    stopped = await handle_lifecycle_macros(
        [MacroInvocation("stop_task", name, (0, 0))], dynamic_ctx,
    )
    body = list(stopped.values())[0].lower()
    assert "fermato" in body
    assert "quale intendi" not in body
    final_state = cont.load_state(cont.state_file_path(name))
    assert final_state["id"] == new_state["id"]
    assert final_state["status"] == "stopped"
    assert len(sched.load_queue()) == 1


def test_add_continuous_purges_only_canceled_continuous_same_name(
    tmp_path, monkeypatch,
):
    import scheduler as sched

    queue = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue)
    queue.write_text(json.dumps([
        {"id": "old", "name": "same", "type": "continuous", "status": "canceled"},
        {"id": "active", "name": "other", "type": "continuous", "status": "pending"},
        {"id": "periodic", "name": "same", "type": "periodic", "status": "canceled"},
    ]))

    sched.add_task({
        "id": "new",
        "name": "same",
        "type": "continuous",
        "agent_file": "agents/same.md",
        "thread_id": "42",
        "state_file": "data/continuous/same/state.json",
    }, allow_legacy_unscoped=True)

    entries = sched.load_queue()
    assert {entry["id"] for entry in entries} == {"new", "active", "periodic"}
