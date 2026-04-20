"""Tests for bot/migrations/v0_23_0.py (spec 005, US4).

The migration rewrites every pre-0.23.0 continuous task state so its
``workspace_thread_id`` points at the parent workspace chat, records the
previous value in ``legacy_workspace_thread_id``, ensures a ``plan.md``
exists, and best-effort closes the legacy sub-topic. The test suite
exercises the happy path, idempotency, platform-side failure modes, state
corruption, missing workspaces, and the offline (platform=None) path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from migrations.base import MigrationContext
import migrations.v0_23_0 as mig


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


class _FakeAgent:
    def __init__(self, name: str, thread_id=None, chat_id=None):
        self.name = name
        self.thread_id = thread_id
        self.chat_id = chat_id


class _FakeManager:
    def __init__(self, agents: dict):
        self._agents = agents

    def get(self, name):
        return self._agents.get(name)


@pytest.fixture
def platform():
    p = AsyncMock()
    p.close_channel = AsyncMock(return_value=True)
    p.send_to_channel = AsyncMock(return_value=True)
    p.send_message = AsyncMock(return_value=True)
    return p


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "continuous").mkdir(parents=True)
    monkeypatch.setattr("continuous.CONTINUOUS_DIR", d / "continuous")
    return d


def _write_state(data_dir: Path, name: str, **overrides) -> Path:
    state = {
        "id": "id-" + name,
        "name": name,
        "status": "running",
        "parent_workspace": "ops",
        "workspace_thread_id": 99,  # legacy sub-topic thread
        "branch": "continuous/" + name,
        "work_dir": "/tmp/x",
        "created_at": "2026-03-01T09:00:00Z",
        "updated_at": "2026-03-01T09:00:00Z",
        "program": {
            "objective": "do the thing",
            "success_criteria": ["criterion 1"],
            "constraints": [],
            "checkpoint_policy": "on-demand",
            "context": "",
        },
        "history": [],
        "total_steps_completed": 0,
    }
    state.update(overrides)
    task_dir = data_dir / "continuous" / name
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "state.json"
    path.write_text(json.dumps(state))
    return path


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_fresh_migration_repoints_state_and_posts_notices(
        self, data_dir, platform,
    ):
        _write_state(data_dir, "daily-report", workspace_thread_id=99)
        _write_state(data_dir, "docs-hunt", workspace_thread_id=77)

        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2, chat_id=500)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        # State for both tasks is repointed at the parent thread (2).
        for name, legacy in (("daily-report", 99), ("docs-hunt", 77)):
            state = json.loads(
                (data_dir / "continuous" / name / "state.json").read_text()
            )
            assert state["workspace_thread_id"] == 2
            assert state["legacy_workspace_thread_id"] == legacy
            assert state["migrated_v0_23_0"]  # ISO timestamp stamped
            assert state["plan_path"].endswith("plan.md")

        # plan.md generated for both.
        for name in ("daily-report", "docs-hunt"):
            plan_path = data_dir / "continuous" / name / "plan.md"
            assert plan_path.exists()

        # Each legacy sub-topic closed once (2 tasks × 1 close each).
        assert platform.close_channel.await_count == 2

        # One transition notice per task posted to the parent thread.
        assert platform.send_message.await_count == 2
        for call in platform.send_message.await_args_list:
            kwargs = call.kwargs
            assert kwargs["thread_id"] == 2
            assert "migrato — da ora riporto qui" in kwargs["text"]

        # Process-wide done marker written.
        assert (data_dir / "migrations" / "v0_23_0.done").exists()


# ─────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_rerun_is_noop_per_task_marker(self, data_dir, platform):
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        # Reset call counters before the second run — but the process-
        # wide done marker exists now, so the second invocation should
        # short-circuit without touching the platform or the state.
        platform.close_channel.reset_mock()
        platform.send_message.reset_mock()

        ctx2 = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx2)

        assert platform.close_channel.await_count == 0
        assert platform.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_rerun_without_done_marker_still_skips_per_task(
        self, data_dir, platform,
    ):
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        # Simulate a partial failure path: remove the done marker so the
        # second run re-enters, but the per-task marker should still
        # short-circuit each already-migrated entry.
        (data_dir / "migrations" / "v0_23_0.done").unlink()
        platform.close_channel.reset_mock()
        platform.send_message.reset_mock()

        ctx2 = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx2)

        assert platform.close_channel.await_count == 0
        assert platform.send_message.await_count == 0


# ─────────────────────────────────────────────────────────────────────────
# close_channel failure → fallback notice in legacy sub-topic
# ─────────────────────────────────────────────────────────────────────────


class TestCloseChannelFailure:
    @pytest.mark.asyncio
    async def test_close_channel_false_triggers_fallback_notice(
        self, data_dir, platform,
    ):
        platform.close_channel = AsyncMock(return_value=False)
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        # Fallback notice posted into the legacy sub-topic (99).
        send_to_channel_calls = platform.send_to_channel.await_args_list
        assert any(
            call.args[0] == 99 and "migrato nel workspace chat" in call.args[1]
            for call in send_to_channel_calls
        )

    @pytest.mark.asyncio
    async def test_close_channel_raising_is_swallowed(
        self, data_dir, platform,
    ):
        platform.close_channel = AsyncMock(side_effect=RuntimeError("boom"))
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        # Must NOT raise
        await mig.upgrade(ctx)

        # State still rewritten.
        state = json.loads(
            (data_dir / "continuous" / "daily-report" / "state.json").read_text()
        )
        assert state["migrated_v0_23_0"]


# ─────────────────────────────────────────────────────────────────────────
# Corrupted state
# ─────────────────────────────────────────────────────────────────────────


class TestCorruptedState:
    @pytest.mark.asyncio
    async def test_corrupted_json_raises_but_good_task_persists(
        self, data_dir, platform,
    ):
        # One valid, one corrupted. The migration raises at the end to
        # halt the chain so the operator notices; the good task's state
        # is still persisted atomically before the raise.
        _write_state(data_dir, "good")
        bad_dir = data_dir / "continuous" / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "state.json").write_text("{not valid json")

        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        with pytest.raises(RuntimeError, match="could not be migrated"):
            await mig.upgrade(ctx)

        # Good one migrated; bad one left alone.
        good_state = json.loads(
            (data_dir / "continuous" / "good" / "state.json").read_text()
        )
        assert good_state.get("migrated_v0_23_0")

        bad_text = (data_dir / "continuous" / "bad" / "state.json").read_text()
        assert bad_text == "{not valid json"

        # Done marker NOT written — chain halts, will retry on next boot.
        assert not (data_dir / "migrations" / "v0_23_0.done").exists()


# ─────────────────────────────────────────────────────────────────────────
# Missing workspace
# ─────────────────────────────────────────────────────────────────────────


class TestMissingWorkspace:
    @pytest.mark.asyncio
    async def test_unknown_parent_workspace_raises(
        self, data_dir, platform,
    ):
        _write_state(data_dir, "orphan", parent_workspace="vanished")
        # Empty manager → no "vanished" agent.
        manager = _FakeManager({})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        with pytest.raises(RuntimeError, match="could not be migrated"):
            await mig.upgrade(ctx)

        # State unchanged — no marker stamped.
        state = json.loads(
            (data_dir / "continuous" / "orphan" / "state.json").read_text()
        )
        assert "migrated_v0_23_0" not in state
        assert "legacy_workspace_thread_id" not in state
        # Platform side effects not attempted for the skipped task.
        assert platform.close_channel.await_count == 0
        assert platform.send_message.await_count == 0
        # Done marker NOT written — chain halts until operator investigates.
        assert not (data_dir / "migrations" / "v0_23_0.done").exists()

    @pytest.mark.asyncio
    async def test_retry_after_workspace_recovers_completes_migration(
        self, data_dir, platform,
    ):
        """On re-run, recovered workspaces finish and done_marker is written."""
        _write_state(data_dir, "orphan", parent_workspace="vanished")

        # First pass: vanished workspace → raise.
        ctx1 = MigrationContext(
            platform=platform, manager=_FakeManager({}), data_dir=data_dir,
        )
        with pytest.raises(RuntimeError):
            await mig.upgrade(ctx1)

        # Operator fixes the workspace. Second pass completes.
        manager = _FakeManager({"vanished": _FakeAgent("vanished", thread_id=5)})
        ctx2 = MigrationContext(
            platform=platform, manager=manager, data_dir=data_dir,
        )
        await mig.upgrade(ctx2)

        state = json.loads(
            (data_dir / "continuous" / "orphan" / "state.json").read_text()
        )
        assert state["migrated_v0_23_0"]
        assert state["workspace_thread_id"] == 5
        assert (data_dir / "migrations" / "v0_23_0.done").exists()


# ─────────────────────────────────────────────────────────────────────────
# Edge: legacy == new
# ─────────────────────────────────────────────────────────────────────────


class TestLegacyEqualsNew:
    @pytest.mark.asyncio
    async def test_no_close_when_legacy_thread_equals_new_thread(
        self, data_dir, platform,
    ):
        # State already points at the parent thread (2).
        _write_state(data_dir, "already-ok", workspace_thread_id=2)
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        # close_channel must NOT be called: nothing to close.
        assert platform.close_channel.await_count == 0
        # Transition notice is still posted once (marker still stamped).
        assert platform.send_message.await_count == 1

        state = json.loads(
            (data_dir / "continuous" / "already-ok" / "state.json").read_text()
        )
        assert state["migrated_v0_23_0"]
        assert state["legacy_workspace_thread_id"] == 2
        assert state["workspace_thread_id"] == 2


# ─────────────────────────────────────────────────────────────────────────
# Offline mode (ctx.platform is None)
# ─────────────────────────────────────────────────────────────────────────


class TestOfflineMode:
    @pytest.mark.asyncio
    async def test_platform_none_still_repoints_state(
        self, data_dir,
    ):
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})

        ctx = MigrationContext(platform=None, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)

        state = json.loads(
            (data_dir / "continuous" / "daily-report" / "state.json").read_text()
        )
        assert state["workspace_thread_id"] == 2
        assert state["legacy_workspace_thread_id"] == 99
        assert state["migrated_v0_23_0"]


# ─────────────────────────────────────────────────────────────────────────
# Done marker
# ─────────────────────────────────────────────────────────────────────────


class TestDoneMarker:
    @pytest.mark.asyncio
    async def test_done_marker_written_on_empty_continuous_dir(
        self, data_dir, platform,
    ):
        # No state files at all.
        manager = _FakeManager({})
        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)
        assert (data_dir / "migrations" / "v0_23_0.done").exists()

    @pytest.mark.asyncio
    async def test_done_marker_written_after_loop(
        self, data_dir, platform,
    ):
        _write_state(data_dir, "daily-report")
        manager = _FakeManager({"ops": _FakeAgent("ops", thread_id=2)})
        ctx = MigrationContext(platform=platform, manager=manager, data_dir=data_dir)
        await mig.upgrade(ctx)
        assert (data_dir / "migrations" / "v0_23_0.done").exists()


# ─────────────────────────────────────────────────────────────────────────
# Migration metadata
# ─────────────────────────────────────────────────────────────────────────


class TestMigrationMetadata:
    def test_version_chain_entries(self):
        assert mig.MIGRATION.from_version == "0.22.2"
        assert mig.MIGRATION.to_version == "0.23.0"
        assert "unify" in mig.MIGRATION.description.lower()
