"""Tests for bot/migrations.py — the migration framework.

v0.15.2 changed the migration signature from ``apply(platform)`` to
``apply(platform, manager)`` so state-mutating migrations can route
their work through ``manager.reset_sessions(...)`` instead of writing
``state.json`` directly (which is silently clobbered by the running
bot's next ``save_state()`` call).
"""

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _patch_migrations_file(tmp_path, monkeypatch):
    """Redirect the migrations tracker to a tmp path and snapshot/restore
    the in-memory legacy registry so every test starts with the
    production set of built-in migrations (fresh state is just a
    seeded tracker).

    The v0.20.12 version chain is pre-seeded at the latest migration's
    ``to_version`` so legacy tests — written when ``run_pending`` only
    ran the legacy registry — still see exactly the legacy entries they
    expect in the ``executed`` list.
    """
    import json
    import migrations as mig_mod
    from migrations import legacy as mig_legacy

    tracker = tmp_path / "data" / "migrations.json"
    monkeypatch.setattr(mig_mod, "MIGRATIONS_FILE", tracker)
    monkeypatch.setattr(mig_legacy, "MIGRATIONS_FILE", tracker)
    tracker.parent.mkdir(parents=True, exist_ok=True)
    discovered = mig_mod.discover("migrations")
    latest = discovered[-1].to_version if discovered else "0.20.11"
    tracker.write_text(json.dumps({
        "_chain_": {"current_version": latest, "history": []},
    }))
    mig_mod.clear_registry_for_tests()
    yield
    mig_mod.clear_registry_for_tests()


@pytest.fixture
def fake_platform():
    return AsyncMock()


# ---------------------------------------------------------------------------
# Fake AgentManager — no disk, records reset_sessions calls
# ---------------------------------------------------------------------------


@dataclass
class _FakeAgent:
    name: str
    session_id: str
    session_started: bool = False
    message_count: int = 0
    thread_id: int | None = None
    work_dir: str | None = None
    description: str | None = None
    agent_type: str = "workspace"
    model: str | None = None
    created_at: float | None = None


@dataclass
class _FakeManager:
    """Minimal stand-in for :class:`AgentManager` used by the migration
    tests. ``reset_sessions`` mutates the in-memory agent objects the same
    way the production AgentManager does, so assertions can run against
    the post-mutation in-memory state — exactly mirroring how the live
    bot reads its agents."""

    agents: dict = field(default_factory=dict)
    reset_calls: list = field(default_factory=list)
    save_count: int = 0

    def reset_sessions(self, agent_names):
        self.reset_calls.append(agent_names)
        if agent_names is None:
            target = list(self.agents.keys())
        else:
            target = [n for n in agent_names if n in self.agents]
        for name in target:
            a = self.agents[name]
            a.session_id = "fresh-" + name
            a.session_started = False
            a.message_count = 0
        self.save_count += 1
        return sorted(target)


@pytest.fixture
def fake_manager():
    return _FakeManager()


# ---------------------------------------------------------------------------
# Decorator + run_pending core
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_decorator_adds_migration(self):
        import migrations as mig

        @mig.migration(id="t-001", description="desc")
        async def _do(platform, manager):
            return True

        assert len(mig._REGISTRY) == 1
        assert mig._REGISTRY[0].id == "t-001"
        assert mig._REGISTRY[0].description == "desc"


class TestRunPending:
    @pytest.mark.asyncio
    async def test_runs_all_pending_on_empty_tracker(self, fake_platform, fake_manager):
        import migrations as mig

        calls = []

        @mig.migration(id="a", description="first")
        async def a(platform, manager):
            calls.append("a")
            return True

        @mig.migration(id="b", description="second")
        async def b(platform, manager):
            calls.append("b")
            return True

        executed = await mig.run_pending(fake_platform, fake_manager)

        assert calls == ["a", "b"]
        assert executed == [("a", "success"), ("b", "success")]

    @pytest.mark.asyncio
    async def test_skips_already_applied(self, fake_platform, fake_manager):
        import migrations as mig

        mig.MIGRATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the chain-up-to-date seed installed by the fixture so
        # the chain runner stays a no-op for this legacy-registry test.
        existing = json.loads(mig.MIGRATIONS_FILE.read_text())
        existing["a"] = {"description": "first", "status": "success"}
        mig.MIGRATIONS_FILE.write_text(json.dumps(existing))

        calls = []

        @mig.migration(id="a", description="first")
        async def a(platform, manager):
            calls.append("a")
            return True

        @mig.migration(id="b", description="second")
        async def b(platform, manager):
            calls.append("b")
            return True

        executed = await mig.run_pending(fake_platform, fake_manager)

        assert calls == ["b"]
        assert executed == [("b", "success")]

    @pytest.mark.asyncio
    async def test_failed_migration_is_recorded_and_skipped_next_time(self, fake_platform, fake_manager):
        """A migration returning False (not raising) is recorded as failed
        and skipped on subsequent boots — we never retry automatically."""
        import migrations as mig

        call_count = 0

        @mig.migration(id="a", description="deliberately fails")
        async def a(platform, manager):
            nonlocal call_count
            call_count += 1
            return False

        first = await mig.run_pending(fake_platform, fake_manager)
        assert first == [("a", "failed")]
        assert call_count == 1

        # Second boot: same registry, same tracker file — must NOT re-run.
        second = await mig.run_pending(fake_platform, fake_manager)
        assert second == []
        assert call_count == 1

        tracker = json.loads(mig.MIGRATIONS_FILE.read_text())
        assert tracker["a"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_exception_is_caught_and_recorded(self, fake_platform, fake_manager):
        import migrations as mig

        @mig.migration(id="boom", description="raises")
        async def boom(platform, manager):
            raise RuntimeError("database on fire")

        executed = await mig.run_pending(fake_platform, fake_manager)
        assert executed == [("boom", "error")]

        tracker = json.loads(mig.MIGRATIONS_FILE.read_text())
        assert tracker["boom"]["status"] == "error"
        assert "database on fire" in tracker["boom"]["error"]

        # Not retried on next boot.
        again = await mig.run_pending(fake_platform, fake_manager)
        assert again == []

    @pytest.mark.asyncio
    async def test_corrupt_tracker_treated_as_empty(self, fake_platform, fake_manager):
        import migrations as mig

        mig.MIGRATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        mig.MIGRATIONS_FILE.write_text("{not valid json")

        @mig.migration(id="a", description="first")
        async def a(platform, manager):
            return True

        executed = await mig.run_pending(fake_platform, fake_manager)
        # A corrupt tracker is treated as empty by *both* layers — the
        # chain runner reseeds from SEED_VERSION and runs the bootstrap
        # migration too. That's the desired recovery behaviour: rebuild
        # the tracker from scratch instead of refusing to boot.
        assert ("a", "success") in executed
        assert ("0.20.11→0.20.12", "ok") in executed

    @pytest.mark.asyncio
    async def test_runs_in_registration_order(self, fake_platform, fake_manager):
        import migrations as mig

        order = []

        @mig.migration(id="first", description="1")
        async def one(platform, manager):
            order.append(1)
            return True

        @mig.migration(id="second", description="2")
        async def two(platform, manager):
            order.append(2)
            return True

        @mig.migration(id="third", description="3")
        async def three(platform, manager):
            order.append(3)
            return True

        await mig.run_pending(fake_platform, fake_manager)
        assert order == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_manager_is_passed_to_each_migration(self, fake_platform, fake_manager):
        """Regression guard: every migration must receive the manager
        instance, otherwise state-mutating migrations cannot reach
        :meth:`AgentManager.reset_sessions` and the v0.15.0 clobber bug
        comes back."""
        import migrations as mig

        seen = []

        @mig.migration(id="x", description="x")
        async def x(platform, manager):
            seen.append((platform, manager))
            return True

        await mig.run_pending(fake_platform, fake_manager)
        assert len(seen) == 1
        assert seen[0][0] is fake_platform
        assert seen[0][1] is fake_manager


# ---------------------------------------------------------------------------
# Channel-rename migrations (unchanged behaviour, new signature)
# ---------------------------------------------------------------------------


class TestRenameToCommandBridgeMigration:
    """The historical 0.12.1 migration must keep working for fresh installs
    that have never run it (e.g. clones that pull straight to a post-0.14
    version)."""

    @pytest.mark.asyncio
    async def test_calls_platform_with_expected_args(self, fake_platform, fake_manager):
        from migrations import _rename_to_command_bridge

        fake_platform.rename_main_channel = AsyncMock(return_value=True)
        result = await _rename_to_command_bridge(fake_platform, fake_manager)

        assert result is True
        fake_platform.rename_main_channel.assert_awaited_once_with(
            display_name="Command Bridge",
            slug="command-bridge",
        )

    @pytest.mark.asyncio
    async def test_propagates_false_on_platform_failure(self, fake_platform, fake_manager):
        from migrations import _rename_to_command_bridge

        fake_platform.rename_main_channel = AsyncMock(return_value=False)
        assert await _rename_to_command_bridge(fake_platform, fake_manager) is False

    @pytest.mark.asyncio
    async def test_does_not_touch_manager(self, fake_platform, fake_manager):
        """Channel-rename migrations must ignore the manager arg
        entirely — they only mutate the platform."""
        from migrations import _rename_to_command_bridge

        fake_platform.rename_main_channel = AsyncMock(return_value=True)
        await _rename_to_command_bridge(fake_platform, fake_manager)
        assert fake_manager.reset_calls == []
        assert fake_manager.save_count == 0


class TestRenameToHeadquartersMigration:
    """The 0.14.0 migration renames the previously-named "Command Bridge"
    control room to "Headquarters". It must run *after* the 0.12.1 migration
    on fresh installs so the channel ends up correctly named in one boot."""

    @pytest.mark.asyncio
    async def test_calls_platform_with_expected_args(self, fake_platform, fake_manager):
        from migrations import _rename_to_headquarters

        fake_platform.rename_main_channel = AsyncMock(return_value=True)
        result = await _rename_to_headquarters(fake_platform, fake_manager)

        assert result is True
        fake_platform.rename_main_channel.assert_awaited_once_with(
            display_name="Headquarters",
            slug="headquarters",
        )

    @pytest.mark.asyncio
    async def test_propagates_false_on_platform_failure(self, fake_platform, fake_manager):
        from migrations import _rename_to_headquarters

        fake_platform.rename_main_channel = AsyncMock(return_value=False)
        assert await _rename_to_headquarters(fake_platform, fake_manager) is False

    def test_runs_after_command_bridge_in_registration_order(self):
        """Both built-in migrations must be present and the headquarters
        rename must come *after* the command-bridge rename — otherwise a
        fresh install would end up named "Command Bridge"."""
        # The conftest clears the in-memory registry on each test; inspect
        # the legacy sub-module directly to see the production decorators
        # as they were registered at import time.
        import importlib
        from migrations import legacy

        importlib.reload(legacy)
        ids = [m.id for m in legacy._REGISTRY]
        assert "0.12.1-rename-main-to-command-bridge" in ids
        assert "0.14.0-rename-command-bridge-to-headquarters" in ids
        assert ids.index("0.14.0-rename-command-bridge-to-headquarters") > ids.index(
            "0.12.1-rename-main-to-command-bridge"
        )


# ---------------------------------------------------------------------------
# v0.15.0 + v0.15.2 session-reset migrations
# ---------------------------------------------------------------------------


class TestResetSessionsForReminderSkill:
    """The v0.15.0 migration. Now routed through the manager so the in-memory
    AgentManager is the source of truth and ``save_state()`` cannot clobber
    the reset (the bug v0.15.2 fixes)."""

    @pytest.mark.asyncio
    async def test_no_manager_is_a_noop_success(self, fake_platform):
        from migrations import _reset_sessions_for_reminder_skill

        result = await _reset_sessions_for_reminder_skill(fake_platform, None)
        assert result is True

    @pytest.mark.asyncio
    async def test_resets_session_fields_for_every_agent(self, fake_platform):
        from migrations import _reset_sessions_for_reminder_skill

        m = _FakeManager(agents={
            "robyx": _FakeAgent(
                name="robyx", session_id="old-uuid-robyx",
                session_started=True, message_count=42, thread_id=1,
            ),
            "assistant": _FakeAgent(
                name="assistant", session_id="old-uuid-assistant",
                session_started=True, message_count=17, thread_id=903,
                work_dir="/Users/rpix/Workspace",
                description="Assistente personale",
                created_at=1775628000.0,
            ),
            "code-reviewer": _FakeAgent(
                name="code-reviewer", session_id="old-uuid-reviewer",
                session_started=True, message_count=3, thread_id=555,
                agent_type="specialist",
            ),
        })

        result = await _reset_sessions_for_reminder_skill(fake_platform, m)
        assert result is True

        # The migration asked the manager for a global reset.
        assert m.reset_calls == [None]

        # Every agent has a fresh session_id and reset bookkeeping.
        for name in ("robyx", "assistant", "code-reviewer"):
            agent = m.agents[name]
            assert agent.session_id == "fresh-" + name
            assert agent.session_started is False
            assert agent.message_count == 0

        # Untouched fields survive verbatim.
        assert m.agents["robyx"].thread_id == 1
        assert m.agents["assistant"].thread_id == 903
        assert m.agents["assistant"].work_dir == "/Users/rpix/Workspace"
        assert m.agents["assistant"].description == "Assistente personale"
        assert m.agents["assistant"].created_at == 1775628000.0
        assert m.agents["code-reviewer"].thread_id == 555
        assert m.agents["code-reviewer"].agent_type == "specialist"

    @pytest.mark.asyncio
    async def test_empty_agents_dict_is_a_noop_success(self, fake_platform):
        from migrations import _reset_sessions_for_reminder_skill

        m = _FakeManager()
        result = await _reset_sessions_for_reminder_skill(fake_platform, m)
        assert result is True
        # Empty manager → reset_sessions(None) was called but did nothing.
        assert m.reset_calls == [None]


class TestResetSessionsAfterClobberFix:
    """The new v0.15.2 migration that re-runs the reset on installs whose
    v0.15.0 migration was clobbered by the running AgentManager."""

    @pytest.mark.asyncio
    async def test_no_manager_is_a_noop_success(self, fake_platform):
        from migrations import _reset_sessions_after_clobber_fix

        result = await _reset_sessions_after_clobber_fix(fake_platform, None)
        assert result is True

    @pytest.mark.asyncio
    async def test_resets_via_manager(self, fake_platform):
        from migrations import _reset_sessions_after_clobber_fix

        m = _FakeManager(agents={
            "assistant": _FakeAgent(
                name="assistant",
                session_id="b2c3d4e5-f6a7-8901-bcde-f12345678901",
                session_started=True, message_count=7, thread_id=903,
            ),
        })

        result = await _reset_sessions_after_clobber_fix(fake_platform, m)
        assert result is True

        # Reset was requested for ALL agents.
        assert m.reset_calls == [None]
        # The hardcoded fake UUID from the user's actual production state
        # is gone — replaced by the manager's fresh UUID.
        assert m.agents["assistant"].session_id == "fresh-assistant"
        assert m.agents["assistant"].session_started is False
        assert m.agents["assistant"].message_count == 0
        # Non-session fields survive.
        assert m.agents["assistant"].thread_id == 903

    def test_runs_after_v0_15_0_in_registration_order(self):
        """0.15.2 must come after 0.15.0 (and after the rename migrations)
        so the boot summary lists them in the correct order."""
        import importlib
        from migrations import legacy

        importlib.reload(legacy)
        ids = [m.id for m in legacy._REGISTRY]
        assert "0.15.0-reset-sessions-for-reminder-skill" in ids
        assert "0.15.2-reset-sessions-after-clobber-fix" in ids
        idx_152 = ids.index("0.15.2-reset-sessions-after-clobber-fix")
        idx_150 = ids.index("0.15.0-reset-sessions-for-reminder-skill")
        idx_hq = ids.index("0.14.0-rename-command-bridge-to-headquarters")
        idx_cb = ids.index("0.12.1-rename-main-to-command-bridge")
        assert idx_152 > idx_150 > idx_hq > idx_cb
