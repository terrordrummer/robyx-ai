"""Tests for collaborative workspace lifecycle commands in handlers.py."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents as agents_mod
from collaborative import CollabStore, CollabWorkspace, Role
from handlers import make_handlers
from i18n import STRINGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_collab_msg(user_id=12345, text="hello", chat_id=-200111, user_name="Alice"):
    msg = MagicMock()
    msg.user_id = user_id
    msg.chat_id = chat_id
    msg.text = text
    msg.thread_id = None
    msg.voice_file_id = None
    msg.command = None
    msg.args = []
    msg.user_name = user_name
    return msg


@pytest.fixture
def collab_store(tmp_path, _patch_env):
    import config as cfg
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def collab_ws(collab_store):
    ws = CollabWorkspace(
        id="collab-test1",
        name="test-collab",
        display_name="Test Collab",
        agent_name="test-collab",
        chat_id=-200111,
        interaction_mode="intelligent",
        status="active",
        created_by=12345,
        roles={"12345": "owner", "99999": "participant", "88888": "operator"},
    )
    collab_store.add(ws)
    return ws


@pytest.fixture
def collab_handlers(agent_manager, claude_backend, collab_store, collab_ws):
    agent = agent_manager.add_agent(
        name="test-collab",
        work_dir="/tmp/test",
        description="test collab agent",
        agent_type="workspace",
    )
    agent.collab_workspace_id = "collab-test1"
    agent_manager.save_state()
    return make_handlers(agent_manager, claude_backend, collab_store)


@pytest.fixture(autouse=True)
def _patch_handler_imports(monkeypatch, tmp_path, _patch_env):
    monkeypatch.setattr(agents_mod, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(agents_mod, "WORKSPACE", tmp_path / "workspace")


@pytest.fixture
def msg_ref():
    return AsyncMock()


# ---------------------------------------------------------------------------
# /role
# ---------------------------------------------------------------------------

class TestCollabRoleCommand:
    @pytest.mark.asyncio
    async def test_role_shows_all_users(self, collab_handlers, mock_platform, msg_ref):
        msg = make_collab_msg(user_id=12345, text="/role")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        mock_platform.reply.assert_called_once()
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text
        assert "participant" in text
        assert "operator" in text

    @pytest.mark.asyncio
    async def test_roles_alias(self, collab_handlers, mock_platform, msg_ref):
        msg = make_collab_msg(user_id=12345, text="/roles")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        mock_platform.reply.assert_called_once()
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text


# ---------------------------------------------------------------------------
# /promote
# ---------------------------------------------------------------------------

class TestCollabPromoteCommand:
    @pytest.mark.asyncio
    async def test_promote_participant_to_operator(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/promote 99999")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        mock_platform.reply.assert_called_once()
        text = mock_platform.reply.call_args[0][1]
        assert "operator" in text
        ws = collab_store.get("collab-test1")
        assert ws.get_role(99999) == Role.OPERATOR

    @pytest.mark.asyncio
    async def test_promote_already_operator(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/promote 88888")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "already" in text.lower()

    @pytest.mark.asyncio
    async def test_promote_unknown_user(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/promote 11111")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "not in" in text.lower()

    @pytest.mark.asyncio
    async def test_promote_denied_for_non_owner(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        mock_platform.is_owner = MagicMock(return_value=False)
        msg = make_collab_msg(user_id=99999, text="/promote 88888", user_name="Bob")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text.lower()

    @pytest.mark.asyncio
    async def test_promote_cannot_change_owner(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/promote 12345")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text.lower()

    @pytest.mark.asyncio
    async def test_promote_no_arg(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/promote")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "usage" in text.lower()


# ---------------------------------------------------------------------------
# /demote
# ---------------------------------------------------------------------------

class TestCollabDemoteCommand:
    @pytest.mark.asyncio
    async def test_demote_operator_to_participant(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/demote 88888")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "participant" in text
        ws = collab_store.get("collab-test1")
        assert ws.get_role(88888) == Role.PARTICIPANT

    @pytest.mark.asyncio
    async def test_demote_already_participant(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/demote 99999")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "already" in text.lower()

    @pytest.mark.asyncio
    async def test_demote_cannot_change_owner(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/demote 12345")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text.lower()

    @pytest.mark.asyncio
    async def test_demote_denied_for_non_owner(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        mock_platform.is_owner = MagicMock(return_value=False)
        msg = make_collab_msg(user_id=88888, text="/demote 99999", user_name="Op")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text.lower()


# ---------------------------------------------------------------------------
# /mode
# ---------------------------------------------------------------------------

class TestCollabModeCommand:
    @pytest.mark.asyncio
    async def test_mode_switch_to_passive(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/mode passive")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "passive" in text
        ws = collab_store.get("collab-test1")
        assert ws.interaction_mode == "passive"

    @pytest.mark.asyncio
    async def test_mode_switch_to_intelligent(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        collab_store.update_interaction_mode("collab-test1", "passive")
        msg = make_collab_msg(user_id=12345, text="/mode intelligent")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "intelligent" in text

    @pytest.mark.asyncio
    async def test_mode_invalid_value(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/mode foobar")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_mode_denied_for_non_owner(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        mock_platform.is_owner = MagicMock(return_value=False)
        msg = make_collab_msg(user_id=99999, text="/mode passive", user_name="Bob")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "owner" in text.lower()


# ---------------------------------------------------------------------------
# /close
# ---------------------------------------------------------------------------

class TestCollabCloseCommand:
    @pytest.mark.asyncio
    async def test_close_by_creator(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/close")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "closed" in text.lower()
        ws = collab_store.get("collab-test1")
        assert ws.status == "closed"

    @pytest.mark.asyncio
    async def test_close_denied_for_non_creator(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        mock_platform.is_owner = MagicMock(return_value=False)
        msg = make_collab_msg(user_id=88888, text="/close", user_name="Op")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        text = mock_platform.reply.call_args[0][1]
        assert "creator" in text.lower()
        ws = collab_store.get("collab-test1")
        assert ws.status == "active"

    @pytest.mark.asyncio
    async def test_close_notifies_hq(
        self, collab_handlers, collab_store, mock_platform, msg_ref,
    ):
        msg = make_collab_msg(user_id=12345, text="/close")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        send_calls = mock_platform.send_message.call_args_list
        hq_calls = [c for c in send_calls if c.kwargs.get("chat_id") == -100999]
        assert len(hq_calls) >= 1


# ---------------------------------------------------------------------------
# Non-command messages still route to AI
# ---------------------------------------------------------------------------

class TestCollabNonCommandRouting:
    @pytest.mark.asyncio
    async def test_regular_message_not_intercepted(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        """Regular (non-command) messages are NOT intercepted by lifecycle
        commands -- they reach the AI processing path. We verify by checking
        that the reply is NOT one of the lifecycle responses."""
        msg = make_collab_msg(user_id=12345, text="hello there")
        # No lifecycle reply should happen for a plain message.
        # The AI call may fail in tests (no real CLI), but the key assertion
        # is that no lifecycle command reply was sent.
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        if mock_platform.reply.called:
            text = mock_platform.reply.call_args[0][1]
            assert "usage" not in text.lower()
            assert "closed" not in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_command_not_intercepted(
        self, collab_handlers, mock_platform, msg_ref,
    ):
        """Unrecognized /commands pass through to the AI agent, not handled
        as lifecycle commands."""
        msg = make_collab_msg(user_id=12345, text="/something_random arg1")
        await collab_handlers["message"](mock_platform, msg, msg_ref)
        if mock_platform.reply.called:
            text = mock_platform.reply.call_args[0][1]
            assert "usage" not in text.lower()
            assert "closed" not in text.lower()
