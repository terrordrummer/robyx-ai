"""Tests for bot/timed_scheduler.py — all branches covered."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config as cfg
import timed_scheduler as ts_mod
from timed_scheduler import (
    _next_run_after,
    add_task,
    cancel_tasks_for_agent_file,
    find_due,
    load_queue,
    migrate_oneshot_from_tasks_md,
    run_timed_cycle,
    save_queue,
)


@pytest.fixture(autouse=True)
def _patch_ts_paths(monkeypatch, tmp_path):
    """Re-bind timed_scheduler module globals to tmp paths."""
    monkeypatch.setattr(ts_mod, "TIMED_QUEUE_FILE", tmp_path / "data" / "timed_queue.json")
    monkeypatch.setattr(ts_mod, "TASKS_FILE", tmp_path / "data" / "tasks.md")
    monkeypatch.setattr(ts_mod, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(ts_mod, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)


# -- Helpers ------------------------------------------------------------------


def _now_iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _make_task(name="t1", task_type="one-shot", offset=-10, **kwargs):
    t = {
        "id": "id-%s" % name,
        "name": name,
        "agent_file": "agents/test.md",
        "prompt": "do stuff",
        "type": task_type,
        "scheduled_at": _now_iso(offset),
        "status": "pending",
        "model": "claude-haiku-4-5-20251001",
    }
    t.update(kwargs)
    return t


# -- load_queue / save_queue --------------------------------------------------


def test_load_queue_missing_file():
    assert load_queue() == []


def test_save_and_load_queue(tmp_path):
    tasks = [_make_task()]
    save_queue(tasks)
    loaded = load_queue()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "t1"


def test_save_queue_is_atomic(tmp_path):
    """save_queue must produce no .tmp file after completion."""
    tasks = [_make_task()]
    save_queue(tasks)
    tmp_file = ts_mod.TIMED_QUEUE_FILE.with_suffix(".tmp")
    assert not tmp_file.exists()


def test_load_queue_corrupt_json(tmp_path):
    ts_mod.TIMED_QUEUE_FILE.write_text("NOT JSON")
    assert load_queue() == []


# -- add_task -----------------------------------------------------------------


def test_add_task_generates_defaults(tmp_path):
    add_task({"name": "x", "agent_file": "agents/a.md", "type": "one-shot",
              "scheduled_at": _now_iso(60), "model": "haiku"})
    q = load_queue()
    assert len(q) == 1
    assert q[0]["status"] == "pending"
    assert "id" in q[0]
    assert "created_at" in q[0]


def test_add_task_rejects_missing_scheduled_at_for_one_shot(tmp_path):
    with pytest.raises(ValueError, match="scheduled_at is required for one-shot tasks"):
        add_task({
            "name": "x",
            "agent_file": "agents/a.md",
            "type": "one-shot",
            "model": "haiku",
        })

    assert load_queue() == []


def test_add_task_rejects_invalid_scheduled_at_for_one_shot(tmp_path):
    with pytest.raises(
        ValueError,
        match="scheduled_at for one-shot tasks must be a valid ISO datetime",
    ):
        add_task({
            "name": "x",
            "agent_file": "agents/a.md",
            "type": "one-shot",
            "scheduled_at": "tomorrow",
            "model": "haiku",
        })

    assert load_queue() == []


def test_add_task_normalizes_naive_scheduled_at_for_one_shot(tmp_path):
    add_task({
        "name": "x",
        "agent_file": "agents/a.md",
        "type": "one-shot",
        "scheduled_at": "2099-06-01T12:00:00",
        "model": "haiku",
    })

    q = load_queue()
    assert q[0]["scheduled_at"] == "2099-06-01T12:00:00+00:00"


def test_add_task_preserves_existing(tmp_path):
    add_task(_make_task("a"))
    add_task(_make_task("b"))
    assert len(load_queue()) == 2


def test_add_task_rejects_invalid_agent_file_ref(tmp_path):
    with pytest.raises(ValueError, match="agent_file must be 'agents/<name>.md' or 'specialists/<name>.md'"):
        add_task({
            "name": "x",
            "agent_file": "../secrets.md",
            "type": "one-shot",
            "scheduled_at": _now_iso(60),
            "model": "haiku",
        })

    assert load_queue() == []


def test_add_task_rejects_invalid_task_name(tmp_path):
    with pytest.raises(ValueError, match="task name must be a single relative path segment"):
        add_task({
            "name": "../escape",
            "agent_file": "agents/a.md",
            "type": "one-shot",
            "scheduled_at": _now_iso(60),
            "model": "haiku",
        })

    assert load_queue() == []


def test_cancel_tasks_for_agent_file_marks_pending_matches_only(tmp_path):
    save_queue([
        _make_task("pending-one", agent_file="agents/alpha.md"),
        _make_task(
            "pending-periodic",
            task_type="periodic",
            agent_file="agents/alpha.md",
            next_run=_now_iso(600),
        ),
        _make_task(
            "already-dispatched",
            agent_file="agents/alpha.md",
            status="dispatched",
        ),
        _make_task("other-agent", agent_file="agents/beta.md"),
    ])

    canceled = cancel_tasks_for_agent_file("agents/alpha.md")

    assert canceled == 2
    tasks = {task["name"]: task for task in load_queue()}
    assert tasks["pending-one"]["status"] == "canceled"
    assert tasks["pending-one"]["canceled_reason"] == "workspace closed"
    assert "canceled_at" in tasks["pending-one"]
    assert tasks["pending-periodic"]["status"] == "canceled"
    assert tasks["already-dispatched"]["status"] == "dispatched"
    assert tasks["other-agent"]["status"] == "pending"


def test_cancel_tasks_for_agent_file_missing_queue_returns_zero(tmp_path):
    assert cancel_tasks_for_agent_file("agents/alpha.md") == 0


# -- find_due -----------------------------------------------------------------


def test_find_due_past_task():
    past = _make_task(offset=-60)
    due = find_due([past])
    assert len(due) == 1


def test_find_due_future_task():
    future = _make_task(offset=3600)
    due = find_due([future])
    assert due == []


def test_find_due_ignores_non_pending():
    dispatched = _make_task(offset=-60, status="dispatched")
    due = find_due([dispatched])
    assert due == []


def test_find_due_uses_next_run():
    task = {
        "id": "x",
        "name": "p1",
        "type": "periodic",
        "next_run": _now_iso(-5),
        "status": "pending",
        "agent_file": "a.md",
    }
    assert len(find_due([task])) == 1


def test_find_due_invalid_date_skipped(caplog):
    task = _make_task(offset=-60)
    task["scheduled_at"] = "not-a-date"
    with caplog.at_level("WARNING"):
        due = find_due([task])
    assert due == []
    assert "Invalid date" in caplog.text


def test_find_due_missing_date():
    task = _make_task()
    del task["scheduled_at"]
    assert find_due([task]) == []


# -- _next_run_after ----------------------------------------------------------


def test_next_run_after_advances_past():
    past = datetime.now(timezone.utc) - timedelta(seconds=100)
    nxt = _next_run_after(past, 60)
    assert nxt > datetime.now(timezone.utc)


def test_next_run_after_multi_interval():
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    nxt = _next_run_after(past, 3600)
    assert nxt > datetime.now(timezone.utc)


# -- run_timed_cycle ----------------------------------------------------------


@pytest.fixture
def agent_file(tmp_path):
    f = tmp_path / "data" / "agents" / "test.md"
    f.parent.mkdir(exist_ok=True)
    f.write_text("# Test Agent\nDo stuff.\n")
    return f


@pytest.fixture
def backend():
    b = MagicMock()
    b.build_spawn_command.return_value = ["echo", "spawned"]
    return b


@pytest.mark.asyncio
async def test_run_timed_cycle_dispatches_due_task(tmp_path, agent_file, backend):
    save_queue([_make_task(offset=-10)])

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=9999)):
        result = await run_timed_cycle(backend)

    assert result["dispatched"] == [("t1", 9999)]
    assert result["errors"] == []

    # One-shot: status must be "dispatched" after the cycle
    q = load_queue()
    assert q[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_run_timed_cycle_passes_platform_to_dispatch(tmp_path, agent_file, backend, mock_platform):
    save_queue([_make_task(offset=-10, thread_id="903")])

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=9999)) as mock_dispatch:
        result = await run_timed_cycle(backend, platform=mock_platform)

    assert result["dispatched"] == [("t1", 9999)]
    assert mock_dispatch.await_args.kwargs["platform"] is mock_platform


@pytest.mark.asyncio
async def test_run_timed_cycle_no_due_tasks(tmp_path, backend):
    save_queue([_make_task(offset=3600)])  # future
    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=1234)):
        result = await run_timed_cycle(backend)
    assert result["dispatched"] == []
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_run_timed_cycle_empty_queue(tmp_path, backend):
    result = await run_timed_cycle(backend)
    assert result == {"dispatched": [], "errors": []}


@pytest.mark.asyncio
async def test_run_timed_cycle_dispatch_error(tmp_path, agent_file, backend):
    save_queue([_make_task(offset=-10)])
    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=None)):
        result = await run_timed_cycle(backend)

    assert result["errors"] == ["t1"]
    q = load_queue()
    assert q[0]["status"] == "error"


@pytest.mark.asyncio
async def test_run_timed_cycle_periodic_renews(tmp_path, agent_file, backend):
    task = _make_task(task_type="periodic", offset=-10, interval_seconds=3600)
    save_queue([task])

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=5555)):
        result = await run_timed_cycle(backend)

    assert result["dispatched"] == [("t1", 5555)]
    q = load_queue()
    assert q[0]["status"] == "pending"     # still pending for next run
    assert "next_run" in q[0]
    # next_run must be in the future
    nxt = datetime.fromisoformat(q[0]["next_run"])
    assert nxt > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_run_timed_cycle_multiple_due(tmp_path, agent_file, backend):
    save_queue([_make_task("a", offset=-10), _make_task("b", offset=-5)])

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=100)):
        result = await run_timed_cycle(backend)

    assert len(result["dispatched"]) == 2


@pytest.mark.asyncio
async def test_run_timed_cycle_preserves_concurrent_append(tmp_path, agent_file, backend):
    save_queue([_make_task("a", offset=-10)])

    async def fake_dispatch(task, backend, platform=None):
        add_task({
            "name": "late",
            "agent_file": "agents/test.md",
            "type": "one-shot",
            "scheduled_at": _now_iso(120),
            "model": "haiku",
        })
        return 100

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(side_effect=fake_dispatch)):
        result = await run_timed_cycle(backend)

    assert result["dispatched"] == [("a", 100)]
    tasks = {task["name"]: task for task in load_queue()}
    assert tasks["a"]["status"] == "dispatched"
    assert tasks["late"]["status"] == "pending"


# -- Jitter recovery (past tasks dispatched on first cycle) -------------------


@pytest.mark.asyncio
async def test_jitter_recovery_past_tasks_dispatched(tmp_path, agent_file, backend):
    """Tasks with scheduled_at far in the past must still be dispatched."""
    old_task = _make_task(offset=-86400 * 3)  # 3 days ago
    save_queue([old_task])

    with patch.object(ts_mod, "dispatch_task", new=AsyncMock(return_value=42)):
        result = await run_timed_cycle(backend)

    assert result["dispatched"] == [("t1", 42)]


@pytest.mark.asyncio
async def test_run_timed_cycle_skips_task_with_live_lock(tmp_path, agent_file, backend):
    save_queue([_make_task(offset=-10)])

    with patch.object(ts_mod, "_check_lock", return_value=(True, 321)), \
         patch.object(ts_mod, "dispatch_task", new=AsyncMock()) as mock_dispatch:
        result = await run_timed_cycle(backend)

    assert result == {"dispatched": [], "errors": []}
    mock_dispatch.assert_not_awaited()
    q = load_queue()
    assert q[0]["status"] == "pending"


# -- migrate_oneshot_from_tasks_md --------------------------------------------


def _write_tasks_md(tmp_path, content):
    (tmp_path / "data" / "tasks.md").write_text(content)


TASKS_MD_WITH_ONESHOT = (
    "| Task | Agent | Type | Frequency | Enabled | Model | Thread ID | Description |\n"
    "|------|-------|------|-----------|---------|-------|-----------|-------------|\n"
    "| remind-dentist | agents/remind-dentist.md | one-shot | 2026-06-01T12:00:00 | yes | haiku | 999 | Dentist reminder |\n"
    "| periodic-job | agents/periodic.md | scheduled | hourly | yes | haiku | - | Hourly job |\n"
)


def test_migrate_oneshot_adds_to_queue(tmp_path):
    _write_tasks_md(tmp_path, TASKS_MD_WITH_ONESHOT)
    n = migrate_oneshot_from_tasks_md()
    assert n == 1
    q = load_queue()
    assert len(q) == 1
    assert q[0]["name"] == "remind-dentist"
    assert q[0]["type"] == "one-shot"
    assert q[0]["status"] == "pending"
    assert q[0]["migrated_from_tasks_md"] is True


def test_migrate_oneshot_disables_in_tasks_md(tmp_path):
    _write_tasks_md(tmp_path, TASKS_MD_WITH_ONESHOT)
    migrate_oneshot_from_tasks_md()
    content = (tmp_path / "data" / "tasks.md").read_text()
    # Row for remind-dentist must now say "| no |"
    assert "| remind-dentist |" in content
    for line in content.splitlines():
        if "remind-dentist" in line:
            assert "| no |" in line


def test_migrate_oneshot_leaves_periodic_untouched(tmp_path):
    _write_tasks_md(tmp_path, TASKS_MD_WITH_ONESHOT)
    migrate_oneshot_from_tasks_md()
    content = (tmp_path / "data" / "tasks.md").read_text()
    for line in content.splitlines():
        if "periodic-job" in line:
            assert "| yes |" in line


def test_migrate_oneshot_idempotent(tmp_path):
    _write_tasks_md(tmp_path, TASKS_MD_WITH_ONESHOT)
    n1 = migrate_oneshot_from_tasks_md()
    n2 = migrate_oneshot_from_tasks_md()
    assert n1 == 1
    assert n2 == 0  # already disabled, not migrated twice
    assert len(load_queue()) == 1


def test_migrate_oneshot_no_tasks_md(tmp_path):
    assert migrate_oneshot_from_tasks_md() == 0


def test_migrate_oneshot_invalid_date_skipped(tmp_path, caplog):
    content = (
        "| Task | Agent | Type | Frequency | Enabled | Model | Thread ID | Description |\n"
        "|------|-------|------|-----------|---------|-------|-----------|-------------|\n"
        "| bad-task | agents/x.md | one-shot | NOT-A-DATE | yes | haiku | - | Bad |\n"
    )
    _write_tasks_md(tmp_path, content)
    with caplog.at_level("WARNING"):
        n = migrate_oneshot_from_tasks_md()
    assert n == 0
    assert load_queue() == []


# -- model preference resolution ---------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_task_resolves_model_alias(tmp_path):
    """``dispatch_task`` must route the queued model through
    ``resolve_model_preference`` so semantic aliases (fast/balanced/powerful)
    become concrete backend model ids before reaching the CLI."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from timed_scheduler import dispatch_task

    agent_dir = ts_mod.DATA_DIR / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "test.md").write_text("brief\n")

    task = _make_task(model="powerful")

    backend = MagicMock()
    backend.build_spawn_command.return_value = ["echo", "go"]

    mock_proc = AsyncMock()
    mock_proc.pid = 17

    with patch(
        "timed_scheduler.resolve_model_preference",
        return_value="resolved-id",
    ) as mock_resolve, patch(
        "timed_scheduler.asyncio.create_subprocess_exec", return_value=mock_proc,
    ):
        await dispatch_task(task, backend)

    mock_resolve.assert_called_once()
    called = backend.build_spawn_command.call_args[1]
    assert called["model"] == "resolved-id"


@pytest.mark.asyncio
async def test_dispatch_task_starts_delivery_watch_when_platform_present(tmp_path):
    from timed_scheduler import dispatch_task

    agent_dir = ts_mod.DATA_DIR / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "test.md").write_text("brief\n")

    task = _make_task(thread_id="903")

    backend = MagicMock()
    backend.build_spawn_command.return_value = ["echo", "go"]

    mock_proc = AsyncMock()
    mock_proc.pid = 17

    with patch(
        "timed_scheduler.asyncio.create_subprocess_exec", return_value=mock_proc,
    ), patch(
        "timed_scheduler.start_task_delivery_watch",
    ) as mock_watch:
        await dispatch_task(task, backend, platform=AsyncMock())

    mock_watch.assert_called_once()
    assert mock_watch.call_args.args[0] == task
    assert mock_watch.call_args.args[1] is mock_proc


@pytest.mark.asyncio
async def test_dispatch_task_uses_target_agent_work_dir_and_memory(agent_manager):
    from timed_scheduler import dispatch_task

    spec_dir = ts_mod.DATA_DIR / "specialists"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "reviewer.md").write_text("brief\n")

    agent_manager.add_agent(
        "reviewer",
        "/reviews/worktree",
        "Reviewer",
        agent_type="specialist",
    )

    task = _make_task(
        name="remind-reviewer-123",
        agent_file="specialists/reviewer.md",
    )

    backend = MagicMock()
    backend.build_spawn_command.return_value = ["echo", "go"]

    mock_proc = AsyncMock()
    mock_proc.pid = 29

    with patch(
        "timed_scheduler.build_memory_context",
        return_value="MEMORY",
    ) as mock_memory, patch(
        "timed_scheduler.asyncio.create_subprocess_exec",
        return_value=mock_proc,
    ) as mock_exec, patch(
        "timed_scheduler.start_task_delivery_watch",
    ):
        await dispatch_task(task, backend)

    mock_memory.assert_called_once_with("reviewer", "specialist", "/reviews/worktree")
    assert backend.build_spawn_command.call_args.kwargs["work_dir"] == "/reviews/worktree"
    assert mock_exec.call_args.kwargs["cwd"] == "/reviews/worktree"
