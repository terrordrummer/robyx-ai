"""Tests for bot/scheduler.py — unified scheduler."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config as cfg
import scheduler as sched_mod
from scheduler import (
    FREQUENCY_SECONDS,
    _next_run_after,
    add_reminder,
    add_task,
    append_log,
    cancel_tasks_for_agent_file,
    check_lock,
    load_queue,
    migrate_to_unified_queue,
    parse_tasks,
    run_scheduler_cycle,
    save_queue,
    validate_one_shot_scheduled_at,
)


@pytest.fixture(autouse=True)
def _patch_scheduler_paths(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", data_dir / "queue.json")
    monkeypatch.setattr(sched_mod, "TIMED_QUEUE_FILE", data_dir / "timed_queue.json")
    monkeypatch.setattr(sched_mod, "TASKS_FILE", data_dir / "tasks.md")
    monkeypatch.setattr(sched_mod, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(sched_mod, "DATA_DIR", data_dir)
    data_dir.mkdir(exist_ok=True)
    (data_dir / "agents").mkdir(exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────


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


def _make_reminder(rid="r-1", offset=-60, **kwargs):
    r = {
        "id": rid,
        "type": "reminder",
        "chat_id": -100999,
        "thread_id": 903,
        "message": "hello",
        "fire_at": _now_iso(offset),
        "status": "pending",
        "attempts": 0,
    }
    r.update(kwargs)
    return r


# ═══════════════════════════════════════════════════════════════════════════
# Queue I/O
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueIO:
    def test_load_queue_missing_file(self):
        assert load_queue() == []

    def test_save_and_load_queue(self):
        entries = [_make_task()]
        save_queue(entries)
        loaded = load_queue()
        assert len(loaded) == 1
        assert loaded[0]["name"] == "t1"

    def test_save_queue_is_atomic(self):
        entries = [_make_task()]
        save_queue(entries)
        tmp_file = sched_mod.QUEUE_FILE.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_load_queue_corrupt_json(self):
        sched_mod.QUEUE_FILE.write_text("NOT JSON")
        assert load_queue() == []


# ═══════════════════════════════════════════════════════════════════════════
# add_task
# ═══════════════════════════════════════════════════════════════════════════


class TestAddTask:
    def test_generates_defaults(self):
        add_task({"name": "x", "agent_file": "agents/a.md", "type": "one-shot",
                  "scheduled_at": _now_iso(60), "model": "haiku"})
        q = load_queue()
        assert len(q) == 1
        assert q[0]["status"] == "pending"
        assert "id" in q[0]
        assert "created_at" in q[0]

    def test_rejects_missing_scheduled_at_for_one_shot(self):
        with pytest.raises(ValueError, match="scheduled_at is required"):
            add_task({"name": "x", "agent_file": "agents/a.md", "type": "one-shot", "model": "haiku"})
        assert load_queue() == []

    def test_rejects_invalid_agent_file_ref(self):
        with pytest.raises(ValueError, match="agent_file must be"):
            add_task({"name": "x", "agent_file": "../secrets.md", "type": "one-shot",
                      "scheduled_at": _now_iso(60), "model": "haiku"})
        assert load_queue() == []

    def test_rejects_invalid_task_name(self):
        with pytest.raises(ValueError, match="task name must be"):
            add_task({"name": "../escape", "agent_file": "agents/a.md", "type": "one-shot",
                      "scheduled_at": _now_iso(60), "model": "haiku"})
        assert load_queue() == []

    def test_preserves_existing(self):
        add_task(_make_task("a"))
        add_task(_make_task("b"))
        assert len(load_queue()) == 2

    def test_normalizes_naive_scheduled_at(self):
        add_task({"name": "x", "agent_file": "agents/a.md", "type": "one-shot",
                  "scheduled_at": "2099-06-01T12:00:00", "model": "haiku"})
        q = load_queue()
        assert q[0]["scheduled_at"] == "2099-06-01T12:00:00+00:00"


# ═══════════════════════════════════════════════════════════════════════════
# add_reminder
# ═══════════════════════════════════════════════════════════════════════════


class TestAddReminder:
    def test_creates_reminder_entry(self):
        add_reminder({
            "message": "test",
            "fire_at": _now_iso(60),
            "chat_id": -100999,
            "thread_id": 903,
        })
        q = load_queue()
        assert len(q) == 1
        assert q[0]["type"] == "reminder"
        assert q[0]["status"] == "pending"
        assert q[0]["attempts"] == 0
        assert "id" in q[0]

    def test_unicode_preserved(self):
        add_reminder({"message": "⏰ caffè ☕", "fire_at": _now_iso(60)})
        q = load_queue()
        assert q[0]["message"] == "⏰ caffè ☕"


# ═══════════════════════════════════════════════════════════════════════════
# cancel_tasks_for_agent_file
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelTasks:
    def test_marks_pending_matches_only(self):
        save_queue([
            _make_task("pending-one", agent_file="agents/alpha.md"),
            _make_task("already-dispatched", agent_file="agents/alpha.md", status="dispatched"),
            _make_task("other-agent", agent_file="agents/beta.md"),
        ])

        canceled = cancel_tasks_for_agent_file("agents/alpha.md")
        assert canceled == 1
        tasks = {t["name"]: t for t in load_queue()}
        assert tasks["pending-one"]["status"] == "canceled"
        assert tasks["already-dispatched"]["status"] == "dispatched"
        assert tasks["other-agent"]["status"] == "pending"

    def test_missing_queue_returns_zero(self):
        assert cancel_tasks_for_agent_file("agents/alpha.md") == 0


# ═══════════════════════════════════════════════════════════════════════════
# check_lock
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckLock:
    """``check_lock`` became async in v0.20.6 (it delegates to the async
    ``process.is_ai_process`` / ``get_process_name``). Tests must ``await``
    it and use :class:`AsyncMock` for the process helpers."""

    @pytest.mark.asyncio
    async def test_no_lock_file(self):
        assert await check_lock("no-task") == (False, None)

    @pytest.mark.asyncio
    async def test_invalid_task_name_is_rejected(self):
        assert await check_lock("../escape") == (False, None)

    @pytest.mark.asyncio
    async def test_invalid_lock_content(self):
        lock_dir = cfg.DATA_DIR / "bad-lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("not-a-number")
        locked, pid = await check_lock("bad-lock")
        assert locked is False
        assert not (lock_dir / "lock").exists()

    @pytest.mark.asyncio
    async def test_alive_pid_ai_process(self):
        lock_dir = cfg.DATA_DIR / "alive-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=True), \
             patch("process.is_ai_process", new=AsyncMock(return_value=True)):
            locked, pid = await check_lock("alive-task")

        assert locked is True
        assert pid == 12345

    @pytest.mark.asyncio
    async def test_dead_pid_cleans_lock(self):
        lock_dir = cfg.DATA_DIR / "dead-task"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("12345 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=False):
            locked, pid = await check_lock("dead-task")

        assert locked is False
        assert not (lock_dir / "lock").exists()


# ═══════════════════════════════════════════════════════════════════════════
# _next_run_after
# ═══════════════════════════════════════════════════════════════════════════


class TestNextRunAfter:
    def test_advances_past(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=100)
        nxt = _next_run_after(past, 60)
        assert nxt > datetime.now(timezone.utc)

    def test_multi_interval(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        nxt = _next_run_after(past, 3600)
        assert nxt > datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# append_log
# ═══════════════════════════════════════════════════════════════════════════


class TestAppendLog:
    def test_appends_entry_with_timestamp(self):
        append_log("test-task — OK — done")
        content = cfg.LOG_FILE.read_text()
        assert "test-task — OK — done" in content
        assert content.startswith("[")


# ═══════════════════════════════════════════════════════════════════════════
# run_scheduler_cycle — tasks
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSchedulerCycleTasks:
    @pytest.fixture
    def agent_file(self):
        f = cfg.DATA_DIR / "agents" / "test.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Test Agent\nDo stuff.\n")
        return f

    @pytest.mark.asyncio
    async def test_dispatches_due_one_shot(self, agent_file):
        save_queue([_make_task(offset=-10)])
        with patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock(return_value=9999)):
            result = await run_scheduler_cycle(MagicMock())
        assert result["dispatched"] == [("t1", 9999)]
        q = load_queue()
        assert q[0]["status"] == "dispatched"

    @pytest.mark.asyncio
    async def test_periodic_renews(self, agent_file):
        task = _make_task(task_type="periodic", offset=-10, interval_seconds=3600)
        save_queue([task])
        with patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock(return_value=5555)):
            result = await run_scheduler_cycle(MagicMock())
        assert result["dispatched"] == [("t1", 5555)]
        q = load_queue()
        assert q[0]["status"] == "pending"
        assert "next_run" in q[0]
        nxt = datetime.fromisoformat(q[0]["next_run"])
        assert nxt > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_no_due_tasks(self):
        save_queue([_make_task(offset=3600)])
        result = await run_scheduler_cycle(MagicMock())
        assert result["dispatched"] == []

    @pytest.mark.asyncio
    async def test_empty_queue(self):
        result = await run_scheduler_cycle(MagicMock())
        assert result == {"dispatched": [], "errors": [], "reminders_sent": 0}

    @pytest.mark.asyncio
    async def test_dispatch_error(self, agent_file):
        save_queue([_make_task(offset=-10)])
        with patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock(return_value=None)):
            result = await run_scheduler_cycle(MagicMock())
        assert result["errors"] == ["t1"]
        q = load_queue()
        assert q[0]["status"] == "error"

    @pytest.mark.asyncio
    async def test_skips_locked_task(self, agent_file):
        save_queue([_make_task(offset=-10)])
        with patch.object(sched_mod, "check_lock", return_value=(True, 321)), \
             patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock()) as mock_spawn:
            result = await run_scheduler_cycle(MagicMock())
        assert result["dispatched"] == []
        mock_spawn.assert_not_awaited()
        q = load_queue()
        assert q[0]["status"] == "pending"


# ═══════════════════════════════════════════════════════════════════════════
# run_scheduler_cycle — reminders
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSchedulerCycleReminders:
    @pytest.mark.asyncio
    async def test_sends_due_reminder(self):
        save_queue([_make_reminder(offset=-60)])
        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})
        result = await run_scheduler_cycle(MagicMock(), platform=platform, default_chat_id=-100999)
        assert result["reminders_sent"] == 1
        platform.send_message.assert_awaited_once()
        q = load_queue()
        assert q[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_future_reminder_not_sent(self):
        save_queue([_make_reminder(offset=3600)])
        platform = AsyncMock()
        result = await run_scheduler_cycle(MagicMock(), platform=platform)
        assert result["reminders_sent"] == 0
        platform.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_failure_retries(self):
        save_queue([_make_reminder(offset=-60)])
        platform = AsyncMock()
        platform.send_message = AsyncMock(side_effect=RuntimeError("network"))
        await run_scheduler_cycle(MagicMock(), platform=platform, default_chat_id=-100999)
        q = load_queue()
        assert q[0]["status"] == "pending"
        assert "claim_token" not in q[0]

    @pytest.mark.asyncio
    async def test_max_attempts_marks_failed(self):
        save_queue([_make_reminder(offset=-60, attempts=10)])
        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})
        await run_scheduler_cycle(MagicMock(), platform=platform, default_chat_id=-100999)
        platform.send_message.assert_not_awaited()
        q = load_queue()
        assert q[0]["status"] == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# run_scheduler_cycle — mixed
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSchedulerCycleMixed:
    @pytest.fixture
    def agent_file(self):
        f = cfg.DATA_DIR / "agents" / "test.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Test\n")
        return f

    @pytest.mark.asyncio
    async def test_dispatches_tasks_and_reminders_in_same_cycle(self, agent_file):
        save_queue([
            _make_task(offset=-10),
            _make_reminder(offset=-60),
        ])
        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})
        with patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock(return_value=42)):
            result = await run_scheduler_cycle(MagicMock(), platform=platform, default_chat_id=-100999)
        assert result["dispatched"] == [("t1", 42)]
        assert result["reminders_sent"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Stale claim recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestStaleClaims:
    @pytest.mark.asyncio
    async def test_stale_dispatching_claim_is_reset(self):
        old_claim = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        save_queue([{
            **_make_task(offset=-60),
            "status": "dispatching",
            "claim_token": "old",
            "claimed_at": old_claim,
        }])
        # The next cycle should reset the stale claim and re-dispatch
        with patch.object(sched_mod, "_spawn_agent_task", new=AsyncMock(return_value=42)):
            result = await run_scheduler_cycle(MagicMock())
        assert result["dispatched"] == [("t1", 42)]

    @pytest.mark.asyncio
    async def test_stale_sending_claim_is_reset(self):
        old_claim = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        save_queue([{
            **_make_reminder(offset=-60),
            "status": "sending",
            "claim_token": "old",
            "claimed_at": old_claim,
        }])
        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})
        await run_scheduler_cycle(MagicMock(), platform=platform, default_chat_id=-100999)
        q = load_queue()
        assert q[0]["status"] == "sent"


# ═══════════════════════════════════════════════════════════════════════════
# parse_tasks (legacy, for backward compat)
# ═══════════════════════════════════════════════════════════════════════════


class TestParseTasks:
    def test_missing_file_returns_empty(self):
        assert parse_tasks() == []

    def test_valid_single_row(self):
        header = (
            "| Task | Agent | Type | Frequency | Enabled | Model | Thread ID | Description |\n"
            "|------|-------|------|-----------|---------|-------|-----------|-------------|\n"
        )
        sched_mod.TASKS_FILE.write_text(
            header + "| my-task | agents/my.md | scheduled | hourly | yes | claude | - | Does stuff |\n"
        )
        result = parse_tasks()
        assert len(result) == 1
        assert result[0]["name"] == "my-task"
        assert result[0]["enabled"] is True


# ═══════════════════════════════════════════════════════════════════════════
# validate_one_shot_scheduled_at
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateScheduledAt:
    def test_rejects_none(self):
        with pytest.raises(ValueError):
            validate_one_shot_scheduled_at(None)

    def test_rejects_dash(self):
        with pytest.raises(ValueError):
            validate_one_shot_scheduled_at("-")

    def test_rejects_none_string(self):
        with pytest.raises(ValueError):
            validate_one_shot_scheduled_at("none")

    def test_normalizes_naive(self):
        result = validate_one_shot_scheduled_at("2099-06-01T12:00:00")
        assert result == "2099-06-01T12:00:00+00:00"


# ═══════════════════════════════════════════════════════════════════════════
# Migration
# ═══════════════════════════════════════════════════════════════════════════


class TestMigration:
    def test_skips_if_queue_exists(self):
        sched_mod.QUEUE_FILE.write_text("[]")
        assert migrate_to_unified_queue() == 0

    def test_migrates_periodic_from_tasks_md(self):
        header = (
            "| Task | Agent | Type | Frequency | Enabled | Model | Thread ID | Description |\n"
            "|------|-------|------|-----------|---------|-------|-----------|-------------|\n"
        )
        sched_mod.TASKS_FILE.write_text(
            header + "| my-job | agents/my.md | scheduled | hourly | yes | fast | 999 | My job |\n"
        )
        n = migrate_to_unified_queue()
        assert n == 1
        q = load_queue()
        assert q[0]["type"] == "periodic"
        assert q[0]["interval_seconds"] == 3600
        assert "next_run" in q[0]
        # Old file should be renamed
        assert not sched_mod.TASKS_FILE.exists()

    def test_migrates_timed_queue(self):
        sched_mod.TIMED_QUEUE_FILE.write_text(json.dumps([
            _make_task("timed-1", offset=-10),
        ]))
        n = migrate_to_unified_queue()
        assert n == 1
        q = load_queue()
        assert q[0]["name"] == "timed-1"
        assert not sched_mod.TIMED_QUEUE_FILE.exists()

    def test_migrates_reminders(self):
        reminders_file = sched_mod.DATA_DIR / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-1",
            "message": "hello",
            "fire_at": _now_iso(60),
            "status": "pending",
        }]))
        n = migrate_to_unified_queue()
        assert n == 1
        q = load_queue()
        assert q[0]["type"] == "reminder"
        assert not reminders_file.exists()

    def test_empty_sources_write_empty_queue(self):
        n = migrate_to_unified_queue()
        assert n == 0
        assert sched_mod.QUEUE_FILE.exists()
        assert load_queue() == []


# ═══════════════════════════════════════════════════════════════════════════
# System prompt contracts
# ═══════════════════════════════════════════════════════════════════════════


class TestSystemPrompts:
    def test_robyx_prompt_has_reminders(self):
        from config import ROBYX_SYSTEM_PROMPT
        assert "## Reminders" in ROBYX_SYSTEM_PROMPT

    def test_workspace_prompt_has_reminders(self):
        from config import WORKSPACE_AGENT_SYSTEM_PROMPT
        assert "## Reminders" in WORKSPACE_AGENT_SYSTEM_PROMPT

    def test_focused_prompt_has_reminders(self):
        from config import FOCUSED_AGENT_SYSTEM_PROMPT
        assert "## Reminders" in FOCUSED_AGENT_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════════════════
# Continuous-task on-demand policy enforcement (v0.24.2)
# ═══════════════════════════════════════════════════════════════════════════


class TestOnDemandAutoDemote:
    """Regression coverage for v0.24.2 fire-and-forget invariant.

    A step agent running under the default ``on-demand`` policy must
    never leave its task parked in ``awaiting-input``. If it does (model
    drift, stale template cached in a subprocess, prompt injection), the
    scheduler auto-demotes the state so the loop keeps running. Other
    policies (``on-uncertainty``, ``on-milestone``, ``every-N-steps``)
    legitimately support awaiting-input and must be left alone.
    """

    def _state(self, policy: str | None, question: str = "still unsure?"):
        return {
            "name": "daily-report",
            "status": "awaiting-input",
            "program": {"checkpoint_policy": policy} if policy else {},
            "awaiting_question": question,
        }

    def test_on_demand_awaiting_input_is_demoted_to_pending(self):
        state = self._state("on-demand")
        mutated = sched_mod._maybe_demote_on_demand_awaiting_input(
            state, "daily-report",
        )
        assert mutated is True
        assert state["status"] == "pending"
        assert "awaiting_question" not in state

    def test_missing_policy_defaults_to_on_demand_and_is_demoted(self):
        state = self._state(None)
        assert sched_mod._maybe_demote_on_demand_awaiting_input(
            state, "daily-report",
        ) is True
        assert state["status"] == "pending"

    def test_on_uncertainty_awaiting_input_is_left_alone(self):
        state = self._state("on-uncertainty")
        assert sched_mod._maybe_demote_on_demand_awaiting_input(
            state, "daily-report",
        ) is False
        assert state["status"] == "awaiting-input"
        assert state["awaiting_question"] == "still unsure?"

    def test_on_milestone_awaiting_input_is_left_alone(self):
        state = self._state("on-milestone")
        assert sched_mod._maybe_demote_on_demand_awaiting_input(
            state, "daily-report",
        ) is False
        assert state["status"] == "awaiting-input"

    def test_non_awaiting_states_are_no_op(self):
        for status in ("pending", "running", "paused", "completed", "error"):
            state = {
                "name": "x",
                "status": status,
                "program": {"checkpoint_policy": "on-demand"},
            }
            assert sched_mod._maybe_demote_on_demand_awaiting_input(
                state, "x",
            ) is False
            assert state["status"] == status
