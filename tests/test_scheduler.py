"""Tests for bot/scheduler.py — all branches covered."""

import asyncio
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

import config as cfg
import scheduler as sched_mod
from scheduler import (
    FREQUENCY_SECONDS,
    append_log,
    check_lock,
    get_last_run,
    is_task_due,
    parse_tasks,
    run_scheduler_cycle,
    spawn_task,
)


@pytest.fixture(autouse=True)
def _patch_scheduler_paths(monkeypatch):
    """Re-bind scheduler module globals to the tmp-patched config values."""
    monkeypatch.setattr(sched_mod, "TASKS_FILE", cfg.TASKS_FILE)
    monkeypatch.setattr(sched_mod, "LOG_FILE", cfg.LOG_FILE)
    monkeypatch.setattr(sched_mod, "DATA_DIR", cfg.DATA_DIR)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_task(
    name="test-task",
    agent_file="agents/test.md",
    task_type="scheduled",
    frequency="hourly",
    enabled=True,
    model="claude-sonnet",
    thread_id="-",
    description="A test task",
):
    return {
        "name": name,
        "agent_file": agent_file,
        "type": task_type,
        "frequency": frequency,
        "enabled": enabled,
        "model": model,
        "thread_id": thread_id,
        "description": description,
    }


TASKS_TABLE_HEADER = (
    "| Task | Agent File | Type | Frequency | Enabled | Model | Thread | Description |\n"
    "|------|-----------|------|-----------|---------|-------|--------|-------------|\n"
)


def _write_tasks_table(rows: list[str]):
    """Write a tasks.md with header + rows."""
    cfg.TASKS_FILE.write_text(TASKS_TABLE_HEADER + "\n".join(rows) + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# parse_tasks
# ═══════════════════════════════════════════════════════════════════════════


class TestParseTasks:
    def test_missing_file_returns_empty(self):
        # TASKS_FILE does not exist by default (tmp_path)
        assert parse_tasks() == []

    def test_empty_file_returns_empty(self):
        cfg.TASKS_FILE.write_text("")
        assert parse_tasks() == []

    def test_valid_single_row(self):
        _write_tasks_table([
            "| my-task | agents/my.md | scheduled | hourly | yes | claude-sonnet | - | Does stuff |"
        ])
        result = parse_tasks()
        assert len(result) == 1
        t = result[0]
        assert t["name"] == "my-task"
        assert t["agent_file"] == "agents/my.md"
        assert t["type"] == "scheduled"
        assert t["frequency"] == "hourly"
        assert t["enabled"] is True
        assert t["model"] == "claude-sonnet"
        assert t["thread_id"] == "-"
        assert t["description"] == "Does stuff"

    def test_disabled_task(self):
        _write_tasks_table([
            "| t1 | agents/t1.md | scheduled | daily | no | claude-sonnet | - | Disabled |"
        ])
        result = parse_tasks()
        assert len(result) == 1
        assert result[0]["enabled"] is False

    def test_row_with_fewer_than_8_columns_is_skipped(self):
        _write_tasks_table([
            "| only | four | cols | here |",
            "| valid | agents/v.md | scheduled | hourly | yes | model | - | ok |",
        ])
        result = parse_tasks()
        assert len(result) == 1
        assert result[0]["name"] == "valid"

    def test_multiple_tasks(self):
        _write_tasks_table([
            "| t1 | agents/t1.md | scheduled | hourly | yes | m1 | - | desc1 |",
            "| t2 | agents/t2.md | one-shot  | 2026-06-01T12:00:00 | yes | m2 | - | desc2 |",
            "| t3 | agents/t3.md | interactive | - | no | m3 | tid | desc3 |",
        ])
        result = parse_tasks()
        assert len(result) == 3
        assert [t["name"] for t in result] == ["t1", "t2", "t3"]

    def test_header_and_separator_lines_are_skipped(self):
        # Only header + separator, no data
        cfg.TASKS_FILE.write_text(TASKS_TABLE_HEADER)
        assert parse_tasks() == []


# ═══════════════════════════════════════════════════════════════════════════
# get_last_run
# ═══════════════════════════════════════════════════════════════════════════


class TestGetLastRun:
    def test_no_log_file_returns_none(self):
        assert get_last_run("some-task") is None

    def test_matching_ok_entry(self):
        cfg.LOG_FILE.write_text("[2026-03-15 10:30] some-task — OK — all good\n")
        result = get_last_run("some-task")
        expected = datetime(2026, 3, 15, 10, 30, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_matching_dispatched_entry(self):
        cfg.LOG_FILE.write_text("[2026-03-15 10:30] some-task — DISPATCHED — Spawned as PID 123\n")
        result = get_last_run("some-task")
        expected = datetime(2026, 3, 15, 10, 30, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_non_matching_entries_return_none(self):
        cfg.LOG_FILE.write_text(
            "[2026-03-15 10:30] other-task — OK — done\n"
            "[2026-03-15 11:00] SCHEDULER — IDLE — No tasks due\n"
        )
        assert get_last_run("some-task") is None

    def test_multiple_entries_returns_last(self):
        cfg.LOG_FILE.write_text(
            "[2026-03-15 08:00] my-task — OK — first\n"
            "[2026-03-15 09:00] my-task — DISPATCHED — second\n"
            "[2026-03-15 10:00] my-task — OK — third\n"
        )
        result = get_last_run("my-task")
        expected = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_invalid_timestamp_is_skipped(self):
        cfg.LOG_FILE.write_text(
            "[not-a-date] my-task — OK — bad\n"
            "[2026-03-15 10:00] my-task — OK — good\n"
        )
        result = get_last_run("my-task")
        expected = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════════
# is_task_due
# ═══════════════════════════════════════════════════════════════════════════


class TestIsTaskDue:
    def test_not_enabled_returns_false(self):
        task = _make_task(enabled=False)
        assert is_task_due(task) is False

    # -- one-shot -- (handled by timed scheduler, never by periodic scheduler)

    def test_one_shot_always_false(self):
        """Periodic scheduler must never dispatch one-shot tasks."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        task = _make_task(task_type="one-shot", frequency=past)
        assert is_task_due(task) is False

    def test_one_shot_future_always_false(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        task = _make_task(task_type="one-shot", frequency=future)
        assert is_task_due(task) is False

    # -- interactive --

    def test_interactive_always_false(self):
        task = _make_task(task_type="interactive", frequency="-")
        assert is_task_due(task) is False

    # -- scheduled --

    def test_scheduled_dash_frequency(self):
        task = _make_task(task_type="scheduled", frequency="-")
        assert is_task_due(task) is False

    def test_scheduled_unknown_frequency_returns_false(self):
        task = _make_task(task_type="scheduled", frequency="every-99h")
        assert is_task_due(task) is False

    def test_scheduled_no_last_run_returns_true(self):
        task = _make_task(task_type="scheduled", frequency="hourly")
        # No log file exists, so get_last_run returns None
        assert is_task_due(task) is True

    def test_scheduled_recent_last_run_not_due(self):
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        cfg.LOG_FILE.write_text("[%s] my-task — OK — done\n" % now_str)
        task = _make_task(name="my-task", task_type="scheduled", frequency="hourly")
        assert is_task_due(task) is False

    def test_scheduled_old_last_run_is_due(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        cfg.LOG_FILE.write_text("[%s] my-task — OK — done\n" % old)
        task = _make_task(name="my-task", task_type="scheduled", frequency="hourly")
        assert is_task_due(task) is True


# ═══════════════════════════════════════════════════════════════════════════
# check_lock
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckLock:
    def test_no_lock_file(self):
        assert check_lock("no-task") == (False, None)

    def test_invalid_task_name_is_rejected(self):
        assert check_lock("../escape") == (False, None)

    def test_invalid_lock_content(self, tmp_path):
        lock_dir = cfg.DATA_DIR / "bad-lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("not-a-number")
        locked, pid = check_lock("bad-lock")
        assert locked is False
        assert pid is None
        assert not (lock_dir / "lock").exists()

    def test_dead_pid_cleans_lock(self, tmp_path):
        lock_dir = cfg.DATA_DIR / "dead-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("99999 2026-01-01T00:00:00Z")

        with patch("scheduler.os.kill", side_effect=OSError("No such process")):
            locked, pid = check_lock("dead-task")

        assert locked is False
        assert pid is None
        assert not (lock_dir / "lock").exists()

    def test_alive_pid_ai_process(self, tmp_path):
        lock_dir = cfg.DATA_DIR / "alive-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=True), \
             patch("process.is_ai_process", return_value=True):
            locked, pid = check_lock("alive-task")

        assert locked is True
        assert pid == 12345
        assert (lock_dir / "lock").exists()

    def test_alive_pid_unrelated_process(self, tmp_path):
        lock_dir = cfg.DATA_DIR / "stale-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=True), \
             patch("process.is_ai_process", return_value=False), \
             patch("process.get_process_name", return_value="vim"):
            locked, pid = check_lock("stale-task")

        assert locked is False
        assert pid is None
        assert not (lock_dir / "lock").exists()

    def test_dead_pid_cleans_lock(self, tmp_path):
        lock_dir = cfg.DATA_DIR / "dead-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=False):
            locked, pid = check_lock("dead-task")

        assert locked is False
        assert pid is None
        assert not (lock_dir / "lock").exists()


# ═══════════════════════════════════════════════════════════════════════════
# spawn_task
# ═══════════════════════════════════════════════════════════════════════════


class TestSpawnTask:
    @pytest.mark.asyncio
    async def test_missing_agent_file_returns_none(self):
        task = _make_task(agent_file="agents/nonexistent.md")
        backend = MagicMock()
        result = await spawn_task(task, backend)
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_invalid_agent_file_ref(self):
        task = _make_task(agent_file="../secrets.md")
        backend = MagicMock()
        result = await spawn_task(task, backend)
        assert result is None
        backend.build_spawn_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_invalid_task_name(self):
        task = _make_task(name="../escape", agent_file="agents/test.md")
        backend = MagicMock()
        result = await spawn_task(task, backend)
        assert result is None
        backend.build_spawn_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_spawn(self, tmp_path):
        # Create agent file
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_file = agent_dir / "test.md"
        agent_file.write_text("Do the thing.\n")

        task = _make_task(agent_file="agents/test.md")

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "hello"]

        mock_proc = AsyncMock()
        mock_proc.pid = 42

        with patch("scheduler.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await spawn_task(task, backend)

        assert result == 42
        lock_file = cfg.DATA_DIR / "test-task" / "lock"
        assert lock_file.exists()
        content = lock_file.read_text()
        assert content.startswith("42 ")

    @pytest.mark.asyncio
    async def test_spawn_exception_returns_none(self, tmp_path):
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "test.md").write_text("instructions\n")

        task = _make_task(agent_file="agents/test.md")
        backend = MagicMock()
        backend.build_spawn_command.return_value = ["false"]

        with patch("scheduler.asyncio.create_subprocess_exec", side_effect=OSError("fail")):
            result = await spawn_task(task, backend)

        assert result is None

    @pytest.mark.asyncio
    async def test_resolves_model_alias_via_model_preferences(self, tmp_path):
        """``spawn_task`` must resolve semantic aliases (fast/balanced/powerful)
        through ``resolve_model_preference`` so the active backend gets the
        concrete model id rather than the literal alias."""
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "test.md").write_text("Do the thing.\n")

        task = _make_task(agent_file="agents/test.md", model="balanced")

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "ok"]

        mock_proc = AsyncMock()
        mock_proc.pid = 7

        with patch(
            "scheduler.resolve_model_preference",
            return_value="resolved-model-id",
        ) as mock_resolve, patch(
            "scheduler.asyncio.create_subprocess_exec", return_value=mock_proc,
        ):
            await spawn_task(task, backend)

        mock_resolve.assert_called_once()
        # The resolved id (not the alias) reaches the backend.
        called_kwargs = backend.build_spawn_command.call_args[1]
        assert called_kwargs["model"] == "resolved-model-id"

    @pytest.mark.asyncio
    async def test_starts_delivery_watch_when_platform_present(self, mock_platform):
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "test.md").write_text("Do the thing.\n")

        task = _make_task(agent_file="agents/test.md", thread_id="903")
        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "ok"]

        mock_proc = AsyncMock()
        mock_proc.pid = 33

        with patch(
            "scheduler.asyncio.create_subprocess_exec", return_value=mock_proc,
        ), patch(
            "scheduler.start_task_delivery_watch",
        ) as mock_watch:
            await spawn_task(task, backend, platform=mock_platform)

        mock_watch.assert_called_once()
        assert mock_watch.call_args.args[0] == task
        assert mock_watch.call_args.args[1] is mock_proc

    @pytest.mark.asyncio
    async def test_uses_stored_agent_work_dir_for_memory_and_spawn(
        self, agent_manager
    ):
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "test.md").write_text("Do the thing.\n")

        agent_manager.add_agent(
            "test",
            "/custom/worktree",
            "Scheduled workspace",
            "workspace",
        )
        task = _make_task(name="nightly-test", agent_file="agents/test.md")

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "ok"]

        mock_proc = AsyncMock()
        mock_proc.pid = 88

        with patch(
            "scheduler.build_memory_context",
            return_value="MEMORY",
        ) as mock_memory, patch(
            "scheduler.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec, patch(
            "scheduler.start_task_delivery_watch",
        ):
            await spawn_task(task, backend)

        mock_memory.assert_called_once_with("test", "workspace", "/custom/worktree")
        assert backend.build_spawn_command.call_args.kwargs["work_dir"] == "/custom/worktree"
        assert mock_exec.call_args.kwargs["cwd"] == "/custom/worktree"


# ═══════════════════════════════════════════════════════════════════════════
# append_log
# ═══════════════════════════════════════════════════════════════════════════


class TestAppendLog:
    def test_appends_entry_with_timestamp(self):
        append_log("test-task — OK — done")
        content = cfg.LOG_FILE.read_text()
        assert "test-task — OK — done" in content
        # Should have timestamp prefix [YYYY-MM-DD HH:MM]
        assert content.startswith("[")
        assert "]" in content



# ═══════════════════════════════════════════════════════════════════════════
# run_scheduler_cycle
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSchedulerCycle:
    @pytest.mark.asyncio
    async def test_no_tasks_logs_idle(self):
        backend = MagicMock()
        result = await run_scheduler_cycle(backend)
        assert result["dispatched"] == []
        assert result["skipped"] == []
        assert result["errors"] == []
        content = cfg.LOG_FILE.read_text()
        assert "IDLE" in content

    @pytest.mark.asyncio
    async def test_disabled_task_not_processed(self):
        _write_tasks_table([
            "| dis | agents/d.md | scheduled | hourly | no | claude | - | disabled |",
        ])
        backend = MagicMock()
        result = await run_scheduler_cycle(backend)
        # Disabled tasks are skipped before is_task_due, so not in skipped list either
        assert result["dispatched"] == []
        assert result["errors"] == []
        # IDLE because no dispatched/errors
        assert "IDLE" in cfg.LOG_FILE.read_text()

    @pytest.mark.asyncio
    async def test_task_not_due_is_skipped(self):
        # Task with recent log entry — not due
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        cfg.LOG_FILE.write_text("[%s] my-task — OK — done\n" % now_str)

        _write_tasks_table([
            "| my-task | agents/m.md | scheduled | hourly | yes | claude | - | desc |",
        ])
        backend = MagicMock()
        result = await run_scheduler_cycle(backend)
        assert result["dispatched"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0][0] == "my-task"

    @pytest.mark.asyncio
    async def test_locked_task_is_skipped(self):
        _write_tasks_table([
            "| locked-task | agents/l.md | scheduled | hourly | yes | claude | - | desc |",
        ])
        # No log file so task is due; create lock
        lock_dir = cfg.DATA_DIR / "locked-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        mock_result = MagicMock()
        mock_result.stdout = "claude"

        backend = MagicMock()
        with patch("process.is_pid_alive", return_value=True), \
             patch("process.is_ai_process", return_value=True):
            result = await run_scheduler_cycle(backend)

        assert result["dispatched"] == []
        assert any("locked-task" == s[0] for s in result["skipped"])
        assert "PID 12345" in cfg.LOG_FILE.read_text()

    @pytest.mark.asyncio
    async def test_task_spawned_successfully(self):
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "ok.md").write_text("Do the work.\n")

        _write_tasks_table([
            "| spawn-task | agents/ok.md | scheduled | hourly | yes | claude | - | desc |",
        ])

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "hi"]

        mock_proc = AsyncMock()
        mock_proc.pid = 77

        with patch("scheduler.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_scheduler_cycle(backend)

        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0] == ("spawn-task", 77)
        assert "DISPATCHED" in cfg.LOG_FILE.read_text()

    @pytest.mark.asyncio
    async def test_passes_platform_through_to_spawn_task(self, mock_platform):
        _write_tasks_table([
            "| spawn-task | agents/ok.md | scheduled | hourly | yes | claude | 903 | desc |",
        ])

        backend = MagicMock()

        with patch(
            "scheduler.spawn_task", new=AsyncMock(return_value=77),
        ) as mock_spawn:
            result = await run_scheduler_cycle(backend, platform=mock_platform)

        assert result["dispatched"] == [("spawn-task", 77)]
        assert mock_spawn.await_args.kwargs["platform"] is mock_platform

    @pytest.mark.asyncio
    async def test_spawn_failure_logged_as_error(self):
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "fail.md").write_text("instructions\n")

        _write_tasks_table([
            "| fail-task | agents/fail.md | scheduled | hourly | yes | claude | - | desc |",
        ])

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["false"]

        with patch("scheduler.asyncio.create_subprocess_exec", side_effect=OSError("boom")):
            result = await run_scheduler_cycle(backend)

        assert len(result["errors"]) == 1
        assert result["errors"][0] == "fail-task"
        assert "ERROR" in cfg.LOG_FILE.read_text()

    @pytest.mark.asyncio
    async def test_one_shot_not_dispatched_by_periodic_scheduler(self):
        """One-shot tasks in tasks.md must be ignored by the periodic scheduler."""
        agent_dir = cfg.DATA_DIR / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "once.md").write_text("Do once.\n")

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_tasks_table([
            "| once-task | agents/once.md | one-shot | %s | yes | claude | - | one-shot desc |" % past,
        ])

        backend = MagicMock()
        backend.build_spawn_command.return_value = ["echo", "ok"]

        result = await run_scheduler_cycle(backend)
        # Periodic scheduler must not touch one-shot tasks
        assert result["dispatched"] == []


# ═══════════════════════════════════════════════════════════════════════════
# get_last_run — invalid timestamp edge case (covers lines 74-75)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetLastRunEdgeCases:
    def test_invalid_timestamp_strptime_raises_value_error(self):
        """A log entry whose timestamp matches the regex pattern but fails strptime.

        The regex expects \\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}, so "9999-99-99 99:99"
        matches the pattern but datetime.strptime raises ValueError. This entry
        should be silently skipped (lines 74-75 in scheduler.py).
        """
        cfg.LOG_FILE.write_text(
            "[9999-99-99 99:99] mytask — OK — bad timestamp\n"
            "[2026-03-15 10:00] mytask — OK — good entry\n"
        )
        result = get_last_run("mytask")
        expected = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc).timestamp()
        assert result == expected

    def test_only_invalid_timestamps_returns_none(self):
        """When ALL matching entries have unparseable timestamps, return None."""
        cfg.LOG_FILE.write_text(
            "[9999-99-99 99:99] onlybad — OK — bad timestamp\n"
        )
        result = get_last_run("onlybad")
        assert result is None
