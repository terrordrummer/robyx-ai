"""Spec 007.1 — /clear command + [GET_ARCHIVE] macro end-to-end.

Covers the handler-layer integration: target-agent resolution from
``msg.thread_id``, orchestrator-HQ refusal, owner-only gating on
workspace/specialist agents, OWNER/OPERATOR gating in collaborative
workspaces, archive emission, session reset, and the
``[GET_ARCHIVE]`` macro that pulls archives back into a future turn.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

import conversations
from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers
from messaging.base import PlatformMessage


@pytest.fixture(autouse=True)
def isolate_conversations_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


def _msg(*, user_id: int, chat_id: int = -100999, thread_id: int | None = None,
         args=None, text: str = "/clear"):
    return PlatformMessage(
        user_id=user_id,
        chat_id=chat_id,
        text=text,
        thread_id=thread_id,
        command="clear",
        args=args or [],
    )


# ── /clear — happy path on a workspace ────────────────────────────────


class TestCmdClearWorkspace:
    @pytest.mark.asyncio
    async def test_archives_history_and_resets_session(
        self, handlers, agent_manager, mock_platform,
    ):
        # Set up a workspace agent and log a few turns.
        agent = agent_manager.add_agent(
            name="atlas",
            work_dir="/tmp",
            description="Atlas Workspace",
            agent_type="workspace",
            thread_id=42,
        )
        original_session = agent.session_id
        conversations.append_turn(
            "atlas", user_text="first message", agent_text="first reply",
        )
        conversations.append_turn(
            "atlas", user_text="second message", agent_text="second reply",
        )

        msg = _msg(user_id=12345, thread_id=42)
        await handlers["clear"](mock_platform, msg, object())

        # Session reset.
        assert agent.session_id != original_session
        assert agent.session_started is False
        assert agent.message_count == 0

        # Archive file exists in the conversations dir.
        archives = list(conversations._agent_dir("atlas").glob("archive-*.md"))
        assert len(archives) == 1
        body = archives[0].read_text()
        assert "first message" in body
        assert "second reply" in body

        # current.jsonl was cleared.
        assert not conversations._current_path("atlas").exists()

    @pytest.mark.asyncio
    async def test_no_history_replies_no_history(
        self, handlers, agent_manager, mock_platform,
    ):
        agent_manager.add_agent(
            name="quiet",
            work_dir="/tmp",
            description="Quiet",
            agent_type="workspace",
            thread_id=99,
        )
        msg = _msg(user_id=12345, thread_id=99)
        await handlers["clear"](mock_platform, msg, object())
        replies = [c.args[1] for c in mock_platform.reply.call_args_list]
        # No-history reply mentions the agent and "No history".
        assert any("quiet" in r and "No history" in r for r in replies)


# ── /clear — non-owner feedback (v0.28.1 hotfix regression guard) ────


class TestCmdClearNonOwnerFeedback:
    """Pre-0.28.1 ``cmd_clear`` returned silently when a non-owner ran
    the command in a workspace/specialist topic (legacy ``@owner_only``
    pattern from ``/reset``). Roberto reported "no feedback" — the silent
    branch was the cause. The hotfix replaces the silent return with an
    explicit ``clear_not_owner`` reply so every invocation produces a
    visible response."""

    @pytest.mark.asyncio
    async def test_non_owner_workspace_gets_explicit_refusal(
        self, handlers, agent_manager, mock_platform,
    ):
        from unittest.mock import MagicMock

        agent_manager.add_agent(
            name="atlas-ws", work_dir="/tmp", description="Atlas Workspace",
            agent_type="workspace", thread_id=4242,
        )
        # mock_platform.is_owner returns True by default — override.
        mock_platform.is_owner = MagicMock(return_value=False)
        msg = _msg(user_id=99999, thread_id=4242)
        await handlers["clear"](mock_platform, msg, object())
        replies = [c.args[1] for c in mock_platform.reply.call_args_list]
        assert replies, "Expected an explicit refusal reply, not silence"
        assert any("reserved for the bot owner" in r for r in replies)


# ── /clear — collab without forum-topic thread_id (v0.28.1 fix) ──────


class TestCmdClearCollabFallback:
    """Pre-0.28.1 ``cmd_clear`` only resolved the target agent via
    ``manager.get_by_thread(msg.thread_id)``. In a collab chat where
    ``msg.thread_id`` is None (Telegram non-supergroup, Discord guild
    without forum topics) the lookup missed and the handler replied
    with ``clear_usage`` instead of operating on the collab's agent.
    The hotfix adds a fallback via ``collab_store.get_by_chat_id``."""

    @pytest.mark.asyncio
    async def test_collab_without_thread_id_resolves_via_chat_id(
        self, handlers, agent_manager, store, mock_platform,
    ):
        agent = agent_manager.add_agent(
            name="collab-flat",
            work_dir="/tmp",
            description="Collab Flat",
            agent_type="workspace",
            thread_id=None,
        )
        ws = CollabWorkspace(
            id="collab-flat", name="collab-flat",
            display_name="Collab Flat",
            agent_name="collab-flat",
            chat_id="-100654321",
            platform="telegram",
            status="active",
            created_by=12345,
            roles={"12345": "owner"},
        )
        store.add(ws)
        conversations.append_turn(
            "collab-flat", user_text="ciao", agent_text="ciao back",
        )
        original_session = agent.session_id

        # thread_id=None — non-forum chat. Pre-hotfix this would have
        # routed to clear_usage; post-hotfix it resolves via collab_ws.
        msg = _msg(user_id=12345, chat_id=-100654321, thread_id=None)
        await handlers["clear"](mock_platform, msg, object())

        # Session reset confirms we reached the work path.
        assert agent.session_id != original_session
        # Archive exists.
        archives = list(
            conversations._agent_dir("collab-flat").glob("archive-*.md"),
        )
        assert len(archives) == 1


# ── /clear — orchestrator refusal ─────────────────────────────────────


class TestCmdClearOrchestratorRefusal:
    @pytest.mark.asyncio
    async def test_refuses_in_hq_topic(
        self, handlers, agent_manager, mock_platform,
    ):
        # The conftest seeds the orchestrator (robyx) automatically on
        # AgentManager construction. HQ on Telegram is identified by
        # ``platform.is_main_thread`` returning True — the mock_platform
        # fixture's predicate is ``thread_id is None`` (matches PTB
        # General-topic semantics). On Discord HQ is identified by the
        # control_channel_id; the abstraction is the same.
        robyx = agent_manager.get("robyx")
        assert robyx is not None
        assert robyx.agent_type == "orchestrator"
        conversations.append_turn(
            "robyx", user_text="hq message", agent_text="hq reply",
        )

        # thread_id=None ≡ HQ on Telegram per mock_platform.is_main_thread.
        msg = _msg(user_id=12345, thread_id=None)
        await handlers["clear"](mock_platform, msg, object())

        replies = [c.args[1] for c in mock_platform.reply.call_args_list]
        assert any("not available in the HQ" in r for r in replies)
        # The HQ history is untouched.
        assert conversations._current_path("robyx").exists()


# ── /clear — usage error ──────────────────────────────────────────────


class TestCmdClearUsage:
    @pytest.mark.asyncio
    async def test_unknown_thread_no_args_replies_usage(
        self, handlers, mock_platform,
    ):
        msg = _msg(user_id=12345, thread_id=9999)  # no matching agent
        await handlers["clear"](mock_platform, msg, object())
        replies = [c.args[1] for c in mock_platform.reply.call_args_list]
        assert any("type `/clear` inside" in r for r in replies)

    @pytest.mark.asyncio
    async def test_explicit_agent_name_resolves_target(
        self, handlers, agent_manager, mock_platform,
    ):
        agent = agent_manager.add_agent(
            name="named-agent",
            work_dir="/tmp",
            description="Named",
            agent_type="specialist",
            thread_id=None,
        )
        conversations.append_turn(
            "named-agent", user_text="hi", agent_text="hello",
        )
        original_session = agent.session_id
        msg = _msg(user_id=12345, thread_id=None, args=["named-agent"])
        await handlers["clear"](mock_platform, msg, object())
        assert agent.session_id != original_session


# ── /clear — collaborative authorisation ──────────────────────────────


class TestCmdClearCollabAuth:
    @pytest.mark.asyncio
    async def test_collab_operator_can_clear(
        self, handlers, agent_manager, store, mock_platform,
    ):
        agent = agent_manager.add_agent(
            name="collab-a",
            work_dir="/tmp",
            description="Collab A",
            agent_type="workspace",
            thread_id=777,
        )
        ws = CollabWorkspace(
            id="collab-a",
            name="collab-a",
            display_name="Collab A",
            agent_name="collab-a",
            chat_id="-100777",
            platform="telegram",
            status="active",
            created_by=99999,
            roles={"99999": "owner", "55555": "operator"},
        )
        store.add(ws)

        conversations.append_turn(
            "collab-a", user_text="hi", agent_text="hello",
        )

        # Operator (user 55555) issues /clear inside the collab chat.
        msg = _msg(user_id=55555, chat_id=-100777, thread_id=777)
        await handlers["clear"](mock_platform, msg, object())

        # Session was reset.
        assert agent.session_started is False
        assert agent.message_count == 0
        # Archive exists.
        archives = list(conversations._agent_dir("collab-a").glob("archive-*.md"))
        assert len(archives) == 1

    @pytest.mark.asyncio
    async def test_collab_participant_refused(
        self, handlers, agent_manager, store, mock_platform,
    ):
        agent_manager.add_agent(
            name="collab-b",
            work_dir="/tmp",
            description="Collab B",
            agent_type="workspace",
            thread_id=888,
        )
        ws = CollabWorkspace(
            id="collab-b",
            name="collab-b",
            display_name="Collab B",
            agent_name="collab-b",
            chat_id="-100888",
            platform="telegram",
            status="active",
            created_by=99999,
            roles={"99999": "owner", "22222": "participant"},
        )
        store.add(ws)

        conversations.append_turn(
            "collab-b", user_text="hi", agent_text="hello",
        )
        # Participant (22222) tries /clear — must be refused.
        msg = _msg(user_id=22222, chat_id=-100888, thread_id=888)
        await handlers["clear"](mock_platform, msg, object())
        # No archive produced.
        assert not list(conversations._agent_dir("collab-b").glob("archive-*.md"))
        # Refusal reply mentions "only the owner".
        replies = [c.args[1] for c in mock_platform.reply.call_args_list]
        assert any("only the owner" in r.lower() for r in replies)


# ── [GET_ARCHIVE] macro ──────────────────────────────────────────────


class TestGetArchiveMacro:
    @pytest.mark.asyncio
    async def test_pulls_archives_for_self(self, handlers):
        # Two archives for the same agent.
        conversations.append_turn(
            "atlas", user_text="msg-1", agent_text="reply-1",
        )
        conversations.archive_and_clear("atlas", display_name="Atlas")
        conversations.append_turn(
            "atlas", user_text="msg-2", agent_text="reply-2",
        )
        conversations.archive_and_clear("atlas", display_name="Atlas")

        response = (
            'Let me check what we did before.\n'
            '[GET_ARCHIVE since="1d"]'
        )
        out = await handlers["_handle_get_archive"](response, "atlas")
        # Macro token removed; result embedded.
        assert "[GET_ARCHIVE" not in out
        assert "Archives for atlas" in out
        assert "msg-1" in out or "reply-1" in out
        assert "msg-2" in out or "reply-2" in out

    @pytest.mark.asyncio
    async def test_explicit_name_pulls_another_agent_archive(self, handlers):
        conversations.append_turn(
            "neighbour", user_text="neighbour-msg", agent_text="neighbour-reply",
        )
        conversations.archive_and_clear("neighbour")
        response = '[GET_ARCHIVE since="2d" name="neighbour"]'
        out = await handlers["_handle_get_archive"](response, "atlas")
        assert "Archives for neighbour" in out
        assert "neighbour-msg" in out

    @pytest.mark.asyncio
    async def test_no_archives_in_window(self, handlers):
        response = '[GET_ARCHIVE since="2h" name="never-talked"]'
        out = await handlers["_handle_get_archive"](response, "atlas")
        assert "No conversation archives" in out

    @pytest.mark.asyncio
    async def test_invalid_duration(self, handlers):
        response = '[GET_ARCHIVE since="garbage"]'
        out = await handlers["_handle_get_archive"](response, "atlas")
        assert "INVALID_DURATION" in out

    @pytest.mark.asyncio
    async def test_invalid_limit(self, handlers):
        response = '[GET_ARCHIVE since="2h" limit="9999"]'
        out = await handlers["_handle_get_archive"](response, "atlas")
        assert "INVALID_LIMIT" in out

    @pytest.mark.asyncio
    async def test_no_macro_returns_response_unchanged(self, handlers):
        response = "Plain agent reply, no macro."
        out = await handlers["_handle_get_archive"](response, "atlas")
        assert out == response
