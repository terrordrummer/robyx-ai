"""Tests for bot/session_lifecycle.py — diff-driven session invalidation.

v0.15.2 reshaped this module: the file-I/O entry point was removed
because writing directly to ``state.json`` while a live AgentManager
holds the pre-mutation copy in memory was clobbered by the next
``save_state()`` call. The new entry point routes everything through
:meth:`AgentManager.reset_sessions`, which mutates the in-memory copy
and persists in a single atomic step.
"""

from dataclasses import dataclass, field

import pytest

from session_lifecycle import (
    GLOBAL_INVALIDATION_FILES,
    agents_to_invalidate,
    invalidate_sessions_via_manager,
)


# ---------------------------------------------------------------------------
# Fake manager — captures reset_sessions calls without touching disk
# ---------------------------------------------------------------------------


@dataclass
class _FakeAgent:
    session_id: str
    session_started: bool = False
    message_count: int = 0


@dataclass
class _FakeManager:
    """A minimal stand-in for :class:`AgentManager` that records every
    ``reset_sessions`` call. Only what ``invalidate_sessions_via_manager``
    needs: ``self.agents`` (a name → object dict) and a ``reset_sessions``
    method that returns the sorted list of names that were reset."""

    agents: dict = field(default_factory=dict)
    reset_calls: list = field(default_factory=list)

    def reset_sessions(self, agent_names):
        # Record the call exactly as the production code would receive it.
        self.reset_calls.append(agent_names)
        if agent_names is None:
            return sorted(self.agents.keys())
        # Filter unknown names the same way AgentManager.reset_sessions does.
        return sorted(n for n in agent_names if n in self.agents)


# ---------------------------------------------------------------------------
# agents_to_invalidate (pure decision function)
# ---------------------------------------------------------------------------

class TestAgentsToInvalidate:
    """The diff-to-targets resolver. Returning ``None`` means "all known
    agents"; returning a (possibly empty) set means "exactly these"."""

    def test_global_trigger_config_py(self):
        result = agents_to_invalidate(
            ["bot/config.py"], known_agent_names={"robyx", "assistant"},
        )
        assert result is None  # global

    def test_global_trigger_ai_invoke_py(self):
        result = agents_to_invalidate(
            ["bot/ai_invoke.py"], known_agent_names={"robyx", "assistant"},
        )
        assert result is None

    def test_global_trigger_files_listed_correctly(self):
        # Sanity guard: the constant must contain at least these two paths.
        assert "bot/config.py" in GLOBAL_INVALIDATION_FILES
        assert "bot/ai_invoke.py" in GLOBAL_INVALIDATION_FILES

    def test_per_agent_brief(self):
        result = agents_to_invalidate(
            ["agents/assistant.md"],
            known_agent_names={"robyx", "assistant", "code-reviewer"},
        )
        assert result == {"assistant"}

    def test_per_specialist_brief(self):
        result = agents_to_invalidate(
            ["specialists/code-reviewer.md"],
            known_agent_names={"robyx", "code-reviewer"},
        )
        assert result == {"code-reviewer"}

    def test_unknown_agent_name_is_ignored(self):
        result = agents_to_invalidate(
            ["agents/ghost.md"],
            known_agent_names={"robyx", "assistant"},
        )
        assert result == set()

    def test_mixed_per_agent_and_per_specialist(self):
        result = agents_to_invalidate(
            ["agents/assistant.md", "specialists/code-reviewer.md"],
            known_agent_names={"robyx", "assistant", "code-reviewer"},
        )
        assert result == {"assistant", "code-reviewer"}

    def test_global_wins_over_per_agent(self):
        result = agents_to_invalidate(
            ["bot/config.py", "agents/assistant.md"],
            known_agent_names={"robyx", "assistant"},
        )
        assert result is None

    def test_unrelated_paths_ignored(self):
        result = agents_to_invalidate(
            ["bot/handlers.py", "tests/test_handlers.py", "README.md"],
            known_agent_names={"robyx", "assistant"},
        )
        assert result == set()

    def test_empty_diff(self):
        result = agents_to_invalidate(
            [], known_agent_names={"robyx", "assistant"},
        )
        assert result == set()

    def test_subdirectory_under_agents_does_not_match(self):
        result = agents_to_invalidate(
            ["agents/legacy/foo.md"], known_agent_names={"foo"},
        )
        assert result == set()

    def test_non_md_in_agents_dir_ignored(self):
        result = agents_to_invalidate(
            ["agents/README.txt", "agents/notes.md.bak"],
            known_agent_names={"README", "notes"},
        )
        assert result == set()


# ---------------------------------------------------------------------------
# invalidate_sessions_via_manager (high-level entry point)
# ---------------------------------------------------------------------------

class TestInvalidateSessionsViaManager:
    """The function the updater calls. Asks the AgentManager to do the
    actual reset (no direct ``state.json`` writes — that's the bug
    v0.15.2 fixes)."""

    def _manager_with(self, *names):
        return _FakeManager(agents={n: _FakeAgent("old-" + n) for n in names})

    def test_no_manager_returns_empty(self):
        result = invalidate_sessions_via_manager(None, ["bot/config.py"])
        assert result == []

    def test_empty_changed_paths_returns_empty(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(m, [])
        assert result == []
        assert m.reset_calls == []  # manager was never asked to do anything

    def test_no_known_agents_returns_empty(self):
        m = _FakeManager(agents={})
        result = invalidate_sessions_via_manager(m, ["bot/config.py"])
        assert result == []
        assert m.reset_calls == []

    def test_global_trigger_calls_reset_with_none(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(m, ["bot/config.py"])
        assert sorted(result) == ["assistant", "robyx"]
        # The crucial assertion: manager.reset_sessions was called with None
        # so it does the global reset path.
        assert m.reset_calls == [None]

    def test_global_trigger_via_ai_invoke(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(m, ["bot/ai_invoke.py"])
        assert sorted(result) == ["assistant", "robyx"]
        assert m.reset_calls == [None]

    def test_per_agent_only_resets_named(self):
        m = self._manager_with("robyx", "assistant", "code-reviewer")
        result = invalidate_sessions_via_manager(m, ["agents/assistant.md"])
        assert result == ["assistant"]
        assert m.reset_calls == [{"assistant"}]

    def test_specialist_brief_only_resets_specialist(self):
        m = self._manager_with("robyx", "code-reviewer")
        result = invalidate_sessions_via_manager(
            m, ["specialists/code-reviewer.md"],
        )
        assert result == ["code-reviewer"]
        assert m.reset_calls == [{"code-reviewer"}]

    def test_irrelevant_paths_do_not_call_reset(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(
            m, ["bot/handlers.py", "tests/test_handlers.py", "README.md"],
        )
        assert result == []
        assert m.reset_calls == []

    def test_unknown_agent_brief_in_diff_does_not_call_reset(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(m, ["agents/ghost.md"])
        assert result == []
        assert m.reset_calls == []

    def test_global_wins_over_per_agent_in_mixed_diff(self):
        m = self._manager_with("robyx", "assistant")
        result = invalidate_sessions_via_manager(
            m, ["bot/config.py", "agents/assistant.md"],
        )
        # Global → reset all, not just assistant.
        assert sorted(result) == ["assistant", "robyx"]
        assert m.reset_calls == [None]

    def test_mixed_per_agent_and_per_specialist(self):
        m = self._manager_with("robyx", "assistant", "code-reviewer")
        result = invalidate_sessions_via_manager(
            m,
            ["agents/assistant.md", "specialists/code-reviewer.md"],
        )
        assert sorted(result) == ["assistant", "code-reviewer"]
        assert len(m.reset_calls) == 1
        assert m.reset_calls[0] == {"assistant", "code-reviewer"}
