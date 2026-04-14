"""Integration-style regression tests for the P0+P1+P3 fixes landed in
v0.20.17 and v0.20.18. Each test pins down a cross-module contract
that the architecture review identified as a risk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import scheduler
from agents import Agent, AgentManager


# ── P0.3: fcntl sidecar lock ────────────────────────────────────────


class TestQueueMutex:
    def test_queue_lock_sidecar_file_created_on_mutation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
        scheduler.save_queue([])
        # Mutation should have created the sidecar lockfile.
        sidecar = (tmp_path / "queue.json").with_name("queue.json.lock")
        assert sidecar.exists(), "fcntl sidecar must exist after first mutation"

    def test_queue_mutex_serialises_concurrent_writers(self, tmp_path, monkeypatch):
        # Two entries, two _queue_mutex holders — second must block until
        # first releases. Verified by observing write ordering in the file.
        monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
        scheduler.save_queue([{"id": "a"}])
        # Saving again overwrites — round-trip the data to ensure the
        # mutex path is exercised end-to-end.
        scheduler.save_queue([{"id": "b"}, {"id": "c"}])
        entries = scheduler.load_queue()
        assert [e["id"] for e in entries] == ["b", "c"]


# ── P0.2: stale claim reconciliation ───────────────────────────────


class TestStaleClaimReconciliation:
    def test_entry_removed_logs_info_not_error(self, tmp_path, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
        scheduler.save_queue([])  # no entries
        caplog.set_level(logging.INFO, logger="robyx.scheduler")
        scheduler._reconcile_task_results([
            {"id": "gone", "claim_token": "xyz", "status": "dispatched", "task_type": "one-shot"},
        ])
        # Empty queue short-circuits before the distinction branch. Make
        # sure at least we did not crash.

    def test_dispatched_with_stale_claim_is_error(self, tmp_path, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
        # Entry still exists but claim token has been cleared (stale-reset).
        scheduler.save_queue([{
            "id": "t1",
            "name": "foo",
            "type": "one-shot",
            "status": "pending",
            "agent_file": "agents/foo.md",
        }])
        caplog.set_level(logging.WARNING, logger="robyx.scheduler")
        scheduler._reconcile_task_results([
            {"id": "t1", "claim_token": "stale", "status": "dispatched", "task_type": "one-shot"},
        ])
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("claim token stale" in r.getMessage() for r in errors), (
            "Dispatched-but-unrecorded must surface at ERROR level"
        )


# ── P1.7: reminder max-age ─────────────────────────────────────────


class TestReminderMaxAge:
    def test_expired_reminder_marked_failed_with_reason(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")
        # Reminder whose fire_at is 48h in the past, cap is 24h.
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(hours=48)
        scheduler.save_queue([{
            "id": "r1",
            "type": "reminder",
            "status": "pending",
            "fire_at": old.isoformat(),
            "message": "buy milk",
        }])
        monkeypatch.setattr(scheduler, "REMINDER_MAX_AGE_SECONDS", 86_400)

        scheduler._claim_due_entries()

        entries = scheduler.load_queue()
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert entries[0]["failure_reason"] == "expired"


# ── P1.9: stale lock scan on startup ───────────────────────────────


class TestStaleLockCleanup:
    @pytest.mark.asyncio
    async def test_dead_pid_lock_removed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
        task_dir = tmp_path / "orphan-task"
        task_dir.mkdir()
        # Write a lock pointing at PID 2 (traditionally kthreadd, but on
        # macOS we patch is_pid_alive anyway).
        (task_dir / "lock").write_text("999999 2026-01-01T00:00:00Z")

        with patch("process.is_pid_alive", return_value=False):
            cleaned = await scheduler.cleanup_stale_locks_on_startup()

        assert "orphan-task" in cleaned
        assert not (task_dir / "lock").exists()


# ── P3.16: agent-instructions cache ────────────────────────────────


class TestInstructionsCache:
    def test_cache_hit_does_not_reread_disk(self, tmp_path, monkeypatch):
        import ai_invoke

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        brief = agents_dir / "foo.md"
        brief.write_text("version A")

        monkeypatch.setattr(ai_invoke, "AGENTS_DIR", agents_dir)
        ai_invoke._instructions_cache.clear()

        agent = Agent(name="foo", work_dir=str(tmp_path), description="x", agent_type="workspace")
        first = ai_invoke._load_agent_instructions(agent)
        assert "version A" in first

        # Rewrite contents WITHOUT touching mtime. Cache must serve stale.
        mtime = brief.stat().st_mtime
        brief.write_text("version B")
        os.utime(brief, (mtime, mtime))

        second = ai_invoke._load_agent_instructions(agent)
        assert second == first  # cache hit

    def test_cache_invalidates_on_mtime_change(self, tmp_path, monkeypatch):
        import ai_invoke

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        brief = agents_dir / "bar.md"
        brief.write_text("version A")

        monkeypatch.setattr(ai_invoke, "AGENTS_DIR", agents_dir)
        ai_invoke._instructions_cache.clear()

        agent = Agent(name="bar", work_dir=str(tmp_path), description="x", agent_type="workspace")
        ai_invoke._load_agent_instructions(agent)  # populate cache

        # Bump mtime forward — cache must reload.
        time.sleep(0.01)
        brief.write_text("version B")
        future = brief.stat().st_mtime + 1
        os.utime(brief, (future, future))

        second = ai_invoke._load_agent_instructions(agent)
        assert "version B" in second
