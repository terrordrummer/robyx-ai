"""Tests for bot/agents.py — Agent model and AgentManager."""

import json
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _patch_agents_module(tmp_path, _patch_env, monkeypatch):
    """Ensure agents module-level STATE_FILE and WORKSPACE point to tmp_path.

    The agents module does ``from config import STATE_FILE, WORKSPACE`` at
    import time, so patching ``config.STATE_FILE`` alone is not enough —
    we must also patch the names already bound inside ``agents``.
    """
    import agents as _agents_mod

    monkeypatch.setattr(_agents_mod, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(_agents_mod, "WORKSPACE", tmp_path / "workspace")


@pytest.fixture
def fresh_manager():
    """Create a fresh AgentManager that uses the patched paths."""
    from agents import AgentManager
    return AgentManager()


# ---------------------------------------------------------------------------
# Agent dataclass
# ---------------------------------------------------------------------------

class TestAgent:
    """Tests for the Agent dataclass."""

    def test_to_dict_contains_all_fields(self):
        from agents import Agent

        a = Agent(name="test", work_dir="/tmp/w", description="desc")
        d = a.to_dict()
        expected_keys = {
            "name", "work_dir", "description", "agent_type", "model",
            "session_id", "created_at", "last_used", "message_count",
            "session_started", "thread_id", "collab_workspace_id",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_excludes_lock_and_busy(self):
        from agents import Agent

        d = Agent(name="x", work_dir="/w", description="d").to_dict()
        assert "lock" not in d
        assert "busy" not in d

    def test_round_trip_to_dict_from_dict(self):
        from agents import Agent

        original = Agent(
            name="roundtrip",
            work_dir="/workspace/rt",
            description="round-trip test",
            agent_type="specialist",
            message_count=42,
            session_started=True,
            thread_id=99,
        )
        restored = Agent.from_dict(original.to_dict())
        assert restored.name == original.name
        assert restored.work_dir == original.work_dir
        assert restored.description == original.description
        assert restored.agent_type == original.agent_type
        assert restored.message_count == original.message_count
        assert restored.session_started == original.session_started
        assert restored.thread_id == original.thread_id

    def test_from_dict_filters_unknown_keys(self):
        from agents import Agent

        d = {
            "name": "a",
            "work_dir": "/w",
            "description": "d",
            "unknown_field": "should be ignored",
            "extra": 123,
        }
        a = Agent.from_dict(d)
        assert a.name == "a"
        assert not hasattr(a, "unknown_field")

    def test_from_dict_filters_lock_and_busy(self):
        from agents import Agent

        d = {
            "name": "a",
            "work_dir": "/w",
            "description": "d",
            "lock": "should_not_pass",
            "busy": True,
        }
        a = Agent.from_dict(d)
        # lock should be an asyncio.Lock, not the string
        assert not isinstance(a.lock, str)
        # busy defaults to False because it is filtered out
        assert a.busy is False

    def test_model_field_round_trips(self):
        """``Agent.model`` is the per-agent model preference. It must
        survive a to_dict/from_dict round trip so state files persist
        the agent's intent across restarts."""
        from agents import Agent

        a = Agent(
            name="m",
            work_dir="/w",
            description="d",
            model="powerful",
        )
        restored = Agent.from_dict(a.to_dict())
        assert restored.model == "powerful"

    def test_model_defaults_to_none(self):
        from agents import Agent

        a = Agent(name="m", work_dir="/w", description="d")
        assert a.model is None


# ---------------------------------------------------------------------------
# AgentManager — initialisation and state persistence
# ---------------------------------------------------------------------------

class TestAgentManagerInit:
    """Tests for AgentManager bootstrap and state loading."""

    def test_robyx_exists_after_init(self, fresh_manager):
        robyx = fresh_manager.get("robyx")
        assert robyx is not None
        assert robyx.agent_type == "orchestrator"
        assert robyx.thread_id == 1

    def test_load_state_missing_file(self, tmp_path):
        """No state file -> manager starts with only robyx."""
        from agents import AgentManager
        import agents as _agents_mod

        # Ensure state file does not exist
        sf = _agents_mod.STATE_FILE
        if sf.exists():
            sf.unlink()
        mgr = AgentManager()
        assert list(mgr.agents.keys()) == ["robyx"]

    def test_load_state_corrupt_json(self, tmp_path):
        """Corrupt JSON -> manager starts with only robyx, AND the
        corrupt file is quarantined so the next save_state() doesn't
        silently overwrite it. Closes the 'lose data forever' bug."""
        from agents import AgentManager
        import agents as _agents_mod

        _agents_mod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _agents_mod.STATE_FILE.write_text("{not valid json!!")
        mgr = AgentManager()
        assert "robyx" in mgr.agents
        # Should only have robyx (corrupt state is discarded).
        assert len(mgr.agents) == 1
        # Original state.json is gone (renamed), replaced by a .corrupt-*
        # sibling that preserves the bad bytes for forensics.
        parent = _agents_mod.STATE_FILE.parent
        siblings = list(parent.glob(_agents_mod.STATE_FILE.name + ".corrupt-*"))
        assert len(siblings) == 1, "expected exactly one quarantined file"
        assert siblings[0].read_text() == "{not valid json!!"
        assert not _agents_mod.STATE_FILE.exists(), (
            "state file should be quarantined, not present — otherwise "
            "the next save_state() would overwrite the original"
        )

    def test_load_state_restores_robyx_session(self, tmp_path):
        """robyx session fields are restored from state, not recreated."""
        from agents import AgentManager
        import agents as _agents_mod

        saved_uuid = "12345678-1234-5678-1234-567812345678"
        state = {
            "agents": {
                "robyx": {
                    "session_id": saved_uuid,
                    "message_count": 55,
                    "session_started": True,
                }
            },
            "focused_agent": None,
        }
        _agents_mod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _agents_mod.STATE_FILE.write_text(json.dumps(state))

        mgr = AgentManager()
        assert mgr.agents["robyx"].session_id == saved_uuid
        assert mgr.agents["robyx"].message_count == 55
        assert mgr.agents["robyx"].session_started is True

    def test_load_state_restores_other_agents(self, tmp_path):
        """Non-robyx agents are rebuilt with from_dict."""
        from agents import AgentManager
        import agents as _agents_mod

        state = {
            "agents": {
                "robyx": {"session_id": "k-sid", "message_count": 0, "session_started": False},
                "builder": {
                    "name": "builder",
                    "work_dir": "/builds",
                    "description": "builds stuff",
                    "agent_type": "workspace",
                    "session_id": "b-sid",
                    "created_at": 1000.0,
                    "last_used": 2000.0,
                    "message_count": 10,
                    "session_started": True,
                    "thread_id": 7,
                },
            },
            "focused_agent": "builder",
        }
        _agents_mod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _agents_mod.STATE_FILE.write_text(json.dumps(state))

        mgr = AgentManager()
        assert "builder" in mgr.agents
        assert mgr.agents["builder"].thread_id == 7
        assert mgr.focused_agent == "builder"

    def test_load_state_restores_focused_agent(self, tmp_path):
        """focused_agent is restored from persisted state."""
        from agents import AgentManager
        import agents as _agents_mod

        state = {
            "agents": {
                "robyx": {"session_id": "k", "message_count": 0, "session_started": False},
                "ops": {
                    "name": "ops",
                    "work_dir": "/ops",
                    "description": "ops agent",
                    "agent_type": "workspace",
                    "session_id": "o-sid",
                    "created_at": 1000.0,
                    "last_used": 2000.0,
                    "message_count": 0,
                    "session_started": False,
                    "thread_id": 3,
                },
            },
            "focused_agent": "ops",
        }
        _agents_mod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _agents_mod.STATE_FILE.write_text(json.dumps(state))

        mgr = AgentManager()
        assert mgr.focused_agent == "ops"


class TestPlaceholderSessionIdSanitisation:
    """Session IDs that cannot be used with the Claude CLI must be
    regenerated at load time. Otherwise the CLI rejects them with
    ``Session ID ... is already in use`` and the bot keeps retrying with
    the same id forever, pinning the typing indicator."""

    def _write_state(self, tmp_path, session_id):
        import agents as _agents_mod

        state = {
            "agents": {
                "robyx": {
                    "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "message_count": 0,
                    "session_started": False,
                },
                "ws": {
                    "name": "ws",
                    "work_dir": "/w",
                    "description": "workspace",
                    "agent_type": "workspace",
                    "session_id": session_id,
                    "message_count": 3,
                    "session_started": True,
                    "thread_id": 42,
                },
            },
            "focused_agent": None,
        }
        _agents_mod.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _agents_mod.STATE_FILE.write_text(json.dumps(state))
        return _agents_mod.STATE_FILE

    def test_sanitises_sequential_placeholder_ids(self, tmp_path):
        from agents import AgentManager

        state_file = self._write_state(
            tmp_path, "00000000-0000-0000-0000-000000000003",
        )

        mgr = AgentManager()
        sid = mgr.agents["ws"].session_id
        # Must NOT keep the placeholder
        assert not sid.startswith("00000000-0000-0000-0000-")
        # Must be a valid UUID
        import uuid
        uuid.UUID(sid)
        # Session must be reset so the next call does not use --resume
        assert mgr.agents["ws"].session_started is False
        assert mgr.agents["ws"].message_count == 0
        # Sanitised state must be persisted
        persisted = json.loads(state_file.read_text())
        assert persisted["agents"]["ws"]["session_id"] == sid

    def test_sanitises_non_uuid_strings(self, tmp_path):
        from agents import AgentManager

        self._write_state(tmp_path, "not-a-uuid")
        mgr = AgentManager()
        import uuid
        uuid.UUID(mgr.agents["ws"].session_id)  # raises if invalid

    def test_keeps_valid_uuid_untouched(self, tmp_path):
        from agents import AgentManager

        valid = "abcdef12-3456-7890-abcd-ef1234567890"
        self._write_state(tmp_path, valid)
        mgr = AgentManager()
        assert mgr.agents["ws"].session_id == valid
        # Non-placeholder state preserves session progress
        assert mgr.agents["ws"].session_started is True
        assert mgr.agents["ws"].message_count == 3


class TestIsPlaceholderSessionId:
    def test_placeholder_detector(self):
        from agents import _is_placeholder_session_id

        assert _is_placeholder_session_id("") is True
        assert _is_placeholder_session_id(None) is True
        assert _is_placeholder_session_id("00000000-0000-0000-0000-000000000001") is True
        assert _is_placeholder_session_id("not-a-uuid") is True
        assert _is_placeholder_session_id("abcdef12-3456-7890-abcd-ef1234567890") is False


# ---------------------------------------------------------------------------
# AgentManager — save / reload cycle
# ---------------------------------------------------------------------------

class TestAgentManagerPersistence:

    def test_save_then_load(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev agent", "workspace", thread_id=10)
        fresh_manager.set_focus("dev")
        fresh_manager.save_state()

        from agents import AgentManager
        mgr2 = AgentManager()
        assert "dev" in mgr2.agents
        assert mgr2.focused_agent == "dev"
        assert mgr2.agents["dev"].thread_id == 10


# ---------------------------------------------------------------------------
# AgentManager — add / remove
# ---------------------------------------------------------------------------

class TestAddRemoveAgents:

    def test_add_new_agent(self, fresh_manager):
        a = fresh_manager.add_agent("web", "/web", "web agent", "workspace", thread_id=5)
        assert a.name == "web"
        assert fresh_manager.get("web") is a

    def test_add_updates_existing_agent(self, fresh_manager):
        fresh_manager.add_agent("web", "/web", "old desc", "workspace", thread_id=5)
        updated = fresh_manager.add_agent("web", "/web", "new desc", "workspace", thread_id=8)
        assert updated.description == "new desc"
        assert updated.thread_id == 8

    def test_add_updates_existing_agent_work_dir(self, fresh_manager):
        fresh_manager.add_agent("web", "/old", "old desc", "workspace", thread_id=5)
        updated = fresh_manager.add_agent("web", "/new", "new desc", "workspace", thread_id=8)
        assert updated.work_dir == "/new"

    def test_add_existing_keeps_thread_if_none(self, fresh_manager):
        fresh_manager.add_agent("web", "/web", "desc", "workspace", thread_id=5)
        updated = fresh_manager.add_agent("web", "/web", "desc2", "workspace", thread_id=None)
        # thread_id should stay 5 because ``thread_id or agent.thread_id``
        assert updated.thread_id == 5

    def test_add_agent_saves_state(self, fresh_manager):
        import agents as _agents_mod

        fresh_manager.add_agent("x", "/x", "x", "workspace")
        assert _agents_mod.STATE_FILE.exists()
        data = json.loads(_agents_mod.STATE_FILE.read_text())
        assert "x" in data["agents"]

    def test_add_agent_persists_model_preference(self, fresh_manager):
        """Newly created agents must record the model preference passed by
        ``topics.create_workspace`` so future invocations honour it without
        the caller having to repeat it."""
        a = fresh_manager.add_agent("y", "/y", "y", "workspace", model="powerful")
        assert a.model == "powerful"
        # Reloaded from state.
        from agents import AgentManager
        reloaded = AgentManager()
        assert reloaded.get("y").model == "powerful"

    def test_add_agent_preserves_existing_model_when_none_supplied(
        self, fresh_manager
    ):
        """Re-adding the same agent without a model arg must NOT erase the
        previously stored preference. This matters when ``add_agent`` is
        used to update only the thread_id (e.g. by ``heal_detached_workspaces``)."""
        fresh_manager.add_agent("z", "/z", "z", "workspace", model="balanced")
        fresh_manager.add_agent("z", "/z", "new desc", "workspace", thread_id=11)
        assert fresh_manager.get("z").model == "balanced"
        assert fresh_manager.get("z").thread_id == 11

    def test_add_agent_can_overwrite_model(self, fresh_manager):
        fresh_manager.add_agent("k", "/k", "k", "workspace", model="fast")
        fresh_manager.add_agent("k", "/k", "k", "workspace", model="powerful")
        assert fresh_manager.get("k").model == "powerful"

    def test_remove_normal_agent(self, fresh_manager):
        fresh_manager.add_agent("tmp", "/tmp", "temp", "workspace")
        assert fresh_manager.remove_agent("tmp") is True
        assert fresh_manager.get("tmp") is None

    def test_remove_robyx_fails(self, fresh_manager):
        assert fresh_manager.remove_agent("robyx") is False
        assert fresh_manager.get("robyx") is not None

    def test_remove_nonexistent_returns_false(self, fresh_manager):
        assert fresh_manager.remove_agent("ghost") is False

    def test_remove_focused_agent_clears_focus(self, fresh_manager):
        fresh_manager.add_agent("f", "/f", "focused", "workspace")
        fresh_manager.set_focus("f")
        assert fresh_manager.focused_agent == "f"
        fresh_manager.remove_agent("f")
        assert fresh_manager.focused_agent is None


# ---------------------------------------------------------------------------
# AgentManager — lookup helpers
# ---------------------------------------------------------------------------

class TestLookups:

    def test_get_existing(self, fresh_manager):
        assert fresh_manager.get("robyx") is not None

    def test_get_missing(self, fresh_manager):
        assert fresh_manager.get("nope") is None

    def test_get_by_thread_found(self, fresh_manager):
        fresh_manager.add_agent("worker", "/w", "w", "workspace", thread_id=42)
        assert fresh_manager.get_by_thread(42).name == "worker"

    def test_get_by_thread_not_found(self, fresh_manager):
        assert fresh_manager.get_by_thread(999) is None

    def test_get_by_thread_excludes_robyx(self, fresh_manager):
        # robyx has thread_id=1 but _rebuild_topic_map excludes it
        assert fresh_manager.get_by_thread(1) is None

    def test_list_active_includes_all(self, fresh_manager):
        fresh_manager.add_agent("a1", "/a1", "a1", "workspace")
        names = {a.name for a in fresh_manager.list_active()}
        assert "robyx" in names
        assert "a1" in names

    def test_list_workspaces(self, fresh_manager):
        fresh_manager.add_agent("ws", "/ws", "ws", "workspace")
        fresh_manager.add_agent("sp", "/sp", "sp", "specialist")
        ws = fresh_manager.list_workspaces()
        names = {a.name for a in ws}
        assert "ws" in names
        assert "sp" not in names
        assert "robyx" not in names  # robyx is orchestrator

    def test_list_specialists(self, fresh_manager):
        fresh_manager.add_agent("ws", "/ws", "ws", "workspace")
        fresh_manager.add_agent("sp", "/sp", "sp", "specialist")
        sp = fresh_manager.list_specialists()
        names = {a.name for a in sp}
        assert "sp" in names
        assert "ws" not in names

    def test_list_workspaces_empty(self, fresh_manager):
        # Only robyx (orchestrator) exists — no workspaces
        assert fresh_manager.list_workspaces() == []

    def test_list_specialists_empty(self, fresh_manager):
        assert fresh_manager.list_specialists() == []


# ---------------------------------------------------------------------------
# AgentManager — find_by_mention
# ---------------------------------------------------------------------------

class TestFindByMention:

    def test_mention_found(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        assert fresh_manager.find_by_mention("hello @dev").name == "dev"

    def test_mention_with_punctuation(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        assert fresh_manager.find_by_mention("hey @dev, how?").name == "dev"
        assert fresh_manager.find_by_mention("ping @dev!").name == "dev"
        assert fresh_manager.find_by_mention("@dev.").name == "dev"

    def test_mention_with_question_mark(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        assert fresh_manager.find_by_mention("@dev?").name == "dev"

    def test_mention_case_insensitive(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        assert fresh_manager.find_by_mention("@Dev hello").name == "dev"

    def test_no_mention(self, fresh_manager):
        assert fresh_manager.find_by_mention("just regular text") is None

    def test_mention_unknown_agent(self, fresh_manager):
        assert fresh_manager.find_by_mention("@nobody hi") is None

    def test_mention_robyx(self, fresh_manager):
        # robyx is always present
        assert fresh_manager.find_by_mention("@robyx status").name == "robyx"


# ---------------------------------------------------------------------------
# AgentManager — focus
# ---------------------------------------------------------------------------

class TestFocus:

    def test_set_focus_valid(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        assert fresh_manager.set_focus("dev") is True
        assert fresh_manager.focused_agent == "dev"

    def test_set_focus_invalid(self, fresh_manager):
        assert fresh_manager.set_focus("nonexistent") is False
        assert fresh_manager.focused_agent is None

    def test_set_focus_on_robyx(self, fresh_manager):
        assert fresh_manager.set_focus("robyx") is True
        assert fresh_manager.focused_agent == "robyx"

    def test_clear_focus(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        fresh_manager.set_focus("dev")
        fresh_manager.clear_focus()
        assert fresh_manager.focused_agent is None

    def test_clear_focus_when_already_none(self, fresh_manager):
        fresh_manager.clear_focus()
        assert fresh_manager.focused_agent is None


# ---------------------------------------------------------------------------
# AgentManager — resolve_agent
# ---------------------------------------------------------------------------

class TestResolveAgent:

    def test_resolve_via_mention(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        agent, text = fresh_manager.resolve_agent("@dev build it")
        assert agent.name == "dev"
        assert "@dev" not in text
        assert "build it" in text

    def test_resolve_mention_strips_word(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        _, text = fresh_manager.resolve_agent("please @dev do this")
        assert "@dev" not in text
        assert "please" in text
        assert "do this" in text

    def test_resolve_mention_at_start(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        agent, text = fresh_manager.resolve_agent("@dev")
        assert agent.name == "dev"
        assert text == ""

    def test_resolve_via_focus(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        fresh_manager.set_focus("dev")
        agent, text = fresh_manager.resolve_agent("build it")
        assert agent.name == "dev"
        assert text == "build it"

    def test_resolve_focus_with_stale_name(self, fresh_manager):
        """If focused_agent name no longer exists, fall back to robyx."""
        fresh_manager.focused_agent = "deleted_agent"
        agent, text = fresh_manager.resolve_agent("hello")
        assert agent.name == "robyx"

    def test_resolve_fallback_to_robyx(self, fresh_manager):
        agent, text = fresh_manager.resolve_agent("just a message")
        assert agent.name == "robyx"
        assert text == "just a message"

    def test_resolve_mention_takes_priority_over_focus(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        fresh_manager.add_agent("ops", "/ops", "ops", "workspace")
        fresh_manager.set_focus("ops")
        agent, _ = fresh_manager.resolve_agent("@dev do stuff")
        assert agent.name == "dev"

    def test_resolve_text_unchanged_for_focus_path(self, fresh_manager):
        """When resolving via focus, text is returned unmodified."""
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        fresh_manager.set_focus("dev")
        _, text = fresh_manager.resolve_agent("hello world")
        assert text == "hello world"

    def test_resolve_text_unchanged_for_robyx_fallback(self, fresh_manager):
        _, text = fresh_manager.resolve_agent("hello world")
        assert text == "hello world"


# ---------------------------------------------------------------------------
# AgentManager — _rebuild_topic_map
# ---------------------------------------------------------------------------

class TestRebuildTopicMap:

    def test_maps_thread_ids_correctly(self, fresh_manager):
        fresh_manager.add_agent("a", "/a", "a", "workspace", thread_id=10)
        fresh_manager.add_agent("b", "/b", "b", "workspace", thread_id=20)
        assert fresh_manager._topic_map[10] == "a"
        assert fresh_manager._topic_map[20] == "b"

    def test_excludes_robyx(self, fresh_manager):
        # robyx has thread_id=1
        assert 1 not in fresh_manager._topic_map

    def test_excludes_agents_without_thread_id(self, fresh_manager):
        fresh_manager.add_agent("no_thread", "/n", "n", "workspace", thread_id=None)
        assert "no_thread" not in fresh_manager._topic_map.values()

    def test_rebuild_after_remove(self, fresh_manager):
        fresh_manager.add_agent("a", "/a", "a", "workspace", thread_id=10)
        assert 10 in fresh_manager._topic_map
        fresh_manager.remove_agent("a")
        assert 10 not in fresh_manager._topic_map


# ---------------------------------------------------------------------------
# AgentManager — get_status_summary
# ---------------------------------------------------------------------------

class TestGetStatusSummary:

    def test_empty_summary(self, fresh_manager):
        from i18n import STRINGS
        # Only robyx exists, which is excluded from summary
        assert fresh_manager.get_status_summary() == STRINGS["no_agents"]

    def test_summary_with_workspace_agent(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "developer agent", "workspace")
        summary = fresh_manager.get_status_summary()
        assert "dev" in summary
        assert "[W]" in summary
        assert "developer agent" in summary

    def test_summary_with_specialist_agent(self, fresh_manager):
        fresh_manager.add_agent("review", "/r", "code reviewer", "specialist")
        summary = fresh_manager.get_status_summary()
        assert "[S]" in summary

    def test_summary_busy_agent(self, fresh_manager):
        fresh_manager.add_agent("worker", "/w", "worker", "workspace")
        fresh_manager.get("worker").busy = True
        summary = fresh_manager.get_status_summary()
        assert "..." in summary

    def test_summary_not_busy_agent(self, fresh_manager):
        fresh_manager.add_agent("idle", "/i", "idle", "workspace")
        fresh_manager.get("idle").busy = False
        summary = fresh_manager.get_status_summary()
        # "o" is the icon for not busy — check it appears before the tag
        assert "o [W]" in summary

    def test_summary_focused_agent_marker(self, fresh_manager):
        fresh_manager.add_agent("dev", "/dev", "dev", "workspace")
        fresh_manager.set_focus("dev")
        summary = fresh_manager.get_status_summary()
        # Focus marker is " *" after the agent name
        assert "*dev* *" in summary

    def test_summary_multiple_agents(self, fresh_manager):
        fresh_manager.add_agent("a", "/a", "agent a", "workspace")
        fresh_manager.add_agent("b", "/b", "agent b", "specialist")
        summary = fresh_manager.get_status_summary()
        assert "agent a" in summary
        assert "agent b" in summary
        assert "[W]" in summary
        assert "[S]" in summary

    def test_summary_excludes_robyx(self, fresh_manager):
        summary = fresh_manager.get_status_summary()
        # With only robyx, summary should be "no agents"
        from i18n import STRINGS
        assert summary == STRINGS["no_agents"]


# ---------------------------------------------------------------------------
# format_age
# ---------------------------------------------------------------------------

class TestFormatAge:

    def test_now(self):
        from agents import format_age
        assert format_age(time.time()) == "now"

    def test_minutes_ago(self):
        from agents import format_age
        result = format_age(time.time() - 300)  # 5 minutes
        assert result == "5m ago"

    def test_hours_ago(self):
        from agents import format_age
        result = format_age(time.time() - 7200)  # 2 hours
        assert result == "2h ago"

    def test_days_ago(self):
        from agents import format_age
        result = format_age(time.time() - 172800)  # 2 days
        assert result == "2d ago"

    def test_boundary_just_under_one_minute(self):
        from agents import format_age
        assert format_age(time.time() - 59) == "now"

    def test_boundary_exactly_one_minute(self):
        from agents import format_age
        assert format_age(time.time() - 60) == "1m ago"

    def test_boundary_just_under_one_hour(self):
        from agents import format_age
        result = format_age(time.time() - 3599)
        assert "m ago" in result

    def test_boundary_exactly_one_hour(self):
        from agents import format_age
        assert format_age(time.time() - 3600) == "1h ago"

    def test_boundary_just_under_one_day(self):
        from agents import format_age
        result = format_age(time.time() - 86399)
        assert "h ago" in result

    def test_boundary_exactly_one_day(self):
        from agents import format_age
        assert format_age(time.time() - 86400) == "1d ago"


# ---------------------------------------------------------------------------
# AgentManager.reset_sessions (v0.15.2)
# ---------------------------------------------------------------------------


class TestResetSessions:
    """The v0.15.2 method that fixes the v0.15.0 / v0.15.1 silent regression
    where ``state.json`` mutations from migrations / the updater were
    clobbered by the running bot's next ``save_state()`` call.

    Every reset must mutate the in-memory ``self.agents`` first and persist
    in a single atomic step. The migration framework and the updater both
    go through this method now."""

    def test_global_reset_with_none(self, fresh_manager):
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_started = True
        a.message_count = 7
        old_a_sid = a.session_id

        b = fresh_manager.add_agent("b", "/b", "agent b", "workspace", thread_id=2)
        b.session_started = True
        b.message_count = 3
        old_b_sid = b.session_id

        result = fresh_manager.reset_sessions(None)
        assert sorted(result) == ["a", "b", "robyx"]

        # In-memory state was mutated.
        assert fresh_manager.agents["a"].session_id != old_a_sid
        assert fresh_manager.agents["a"].session_started is False
        assert fresh_manager.agents["a"].message_count == 0
        assert fresh_manager.agents["b"].session_id != old_b_sid
        assert fresh_manager.agents["b"].session_started is False
        assert fresh_manager.agents["b"].message_count == 0
        # Untouched fields survive.
        assert fresh_manager.agents["a"].thread_id == 1
        assert fresh_manager.agents["b"].thread_id == 2

    def test_partial_reset_only_named_agents(self, fresh_manager):
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_started = True
        a.message_count = 7
        b = fresh_manager.add_agent("b", "/b", "agent b", "workspace", thread_id=2)
        b.session_started = True
        b.message_count = 3
        old_a_sid = a.session_id
        old_b_sid = b.session_id

        result = fresh_manager.reset_sessions({"a"})
        assert result == ["a"]

        # Only `a` was reset.
        assert fresh_manager.agents["a"].session_id != old_a_sid
        assert fresh_manager.agents["a"].session_started is False
        assert fresh_manager.agents["a"].message_count == 0
        # `b` and `robyx` are untouched.
        assert fresh_manager.agents["b"].session_id == old_b_sid
        assert fresh_manager.agents["b"].session_started is True
        assert fresh_manager.agents["b"].message_count == 3

    def test_unknown_target_name_is_silently_ignored(self, fresh_manager):
        result = fresh_manager.reset_sessions({"ghost", "phantom"})
        assert result == []

    def test_empty_target_set_resets_nothing(self, fresh_manager):
        result = fresh_manager.reset_sessions(set())
        assert result == []

    def test_reset_persists_to_disk(self, fresh_manager, tmp_path):
        """The reset must immediately call save_state() so a subsequent
        boot picks up the fresh session_id from state.json."""
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_started = True
        a.message_count = 5

        fresh_manager.reset_sessions(None)

        from agents import AgentManager
        new_manager = AgentManager()
        # The fresh session_id from the reset is what the new manager loads.
        assert new_manager.agents["a"].session_id == fresh_manager.agents["a"].session_id
        assert new_manager.agents["a"].session_started is False
        assert new_manager.agents["a"].message_count == 0


class TestResetSessionsSurvivesSubsequentSaveState:
    """**Regression test for the v0.15.0 / v0.15.1 silent failure.**

    The v0.15.0 migration mutated ``state.json`` directly while the running
    AgentManager held the pre-mutation copy in memory. On the very next
    ``save_state()`` call from any interaction, the AgentManager wrote
    its in-memory copy back to disk and **clobbered the migration's
    mutation**. The migration was tracked as ``success`` in
    ``data/migrations.json`` but the agents kept running with the old
    session_id forever.

    These tests would have caught the bug. They simulate the exact
    failure mode: load a manager with a stale session_id, run the
    reset, simulate a downstream interaction that calls save_state,
    then verify that the on-disk state still has the fresh session_id —
    not the stale one. The v0.15.2 fix routes the reset through
    :meth:`AgentManager.reset_sessions` so the in-memory and on-disk
    copies are always in sync."""

    def test_reset_then_unrelated_save_keeps_fresh_session_id(self, fresh_manager, tmp_path):
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_id = "stale-fixed-uuid"
        a.session_started = True
        a.message_count = 7
        fresh_manager.save_state()

        # Trigger the reset (this is what the migration / updater do).
        fresh_manager.reset_sessions(None)
        fresh_session_id = fresh_manager.agents["a"].session_id
        assert fresh_session_id != "stale-fixed-uuid"

        # Simulate a downstream interaction: the agent gets used again
        # and the bot calls save_state() to record the increment. Before
        # v0.15.2 this clobbered the migration's mutation.
        fresh_manager.agents["a"].message_count += 1
        fresh_manager.save_state()

        # On disk: the fresh session_id is still there.
        on_disk = json.loads(
            (tmp_path / "data" / "state.json").read_text()
        )
        assert on_disk["agents"]["a"]["session_id"] == fresh_session_id
        assert on_disk["agents"]["a"]["session_id"] != "stale-fixed-uuid"

    def test_reset_then_reload_picks_up_fresh_session_id(self, fresh_manager, tmp_path):
        """A second AgentManager instantiated after the reset must read
        the fresh session_id from disk — not the stale one."""
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_id = "stale-fixed-uuid"
        a.session_started = True
        a.message_count = 12
        fresh_manager.save_state()

        fresh_manager.reset_sessions({"a"})
        fresh_session_id = fresh_manager.agents["a"].session_id

        from agents import AgentManager
        reloaded = AgentManager()
        assert reloaded.agents["a"].session_id == fresh_session_id
        assert reloaded.agents["a"].session_started is False
        assert reloaded.agents["a"].message_count == 0

    def test_direct_state_json_mutation_would_be_clobbered(self, fresh_manager, tmp_path):
        """**This test demonstrates exactly why v0.15.2 exists.**

        It mutates ``state.json`` directly (the v0.15.0 / v0.15.1 path)
        and proves that a subsequent save_state() from the unchanged
        in-memory AgentManager wipes the mutation out. If this test
        ever starts failing it means someone "fixed" the bug by making
        save_state() smarter, in which case the assertion needs to be
        revisited."""
        a = fresh_manager.add_agent("a", "/a", "agent a", "workspace", thread_id=1)
        a.session_id = "stale-fixed-uuid"
        a.session_started = True
        a.message_count = 7
        fresh_manager.save_state()

        # Mutate state.json on disk WITHOUT going through the manager
        # (this is exactly what the v0.15.0 / v0.15.1 code did).
        state_file = tmp_path / "data" / "state.json"
        state = json.loads(state_file.read_text())
        state["agents"]["a"]["session_id"] = "would-be-fresh-uuid"
        state["agents"]["a"]["session_started"] = False
        state["agents"]["a"]["message_count"] = 0
        state_file.write_text(json.dumps(state, indent=2))

        # Simulate the downstream save_state from any interaction.
        fresh_manager.agents["a"].message_count += 1
        fresh_manager.save_state()

        # The direct mutation was clobbered — exactly the v0.15.0 bug.
        on_disk = json.loads(state_file.read_text())
        assert on_disk["agents"]["a"]["session_id"] == "stale-fixed-uuid"
        assert on_disk["agents"]["a"]["session_id"] != "would-be-fresh-uuid"
