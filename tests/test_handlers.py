"""Tests for bot.handlers — command handlers, owner_only decorator, message routing."""

import json
import unittest.mock
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents as agents_mod
from handlers import make_handlers, owner_only
from i18n import STRINGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_message(user_id=12345, text="hello", thread_id=None, voice_file_id=None, args=None):
    """Create a PlatformMessage-like object for testing."""
    msg = MagicMock()
    msg.user_id = user_id
    msg.chat_id = -100999
    msg.text = text
    msg.thread_id = thread_id
    msg.voice_file_id = voice_file_id
    msg.command = None
    msg.args = args or []
    return msg


def make_voice_message(user_id=12345, thread_id=None):
    return make_message(user_id=user_id, text=None, thread_id=thread_id, voice_file_id="test-file-id")


@pytest.fixture(autouse=True)
def _patch_handler_imports(monkeypatch, tmp_path, _patch_env):
    """Ensure agents.STATE_FILE and agents.WORKSPACE use test values."""
    monkeypatch.setattr(agents_mod, "STATE_FILE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(agents_mod, "WORKSPACE", tmp_path / "workspace")


@pytest.fixture
def handlers(agent_manager, claude_backend):
    return make_handlers(agent_manager, claude_backend)


@pytest.fixture
def msg_ref():
    """An opaque mock message reference for reply/edit."""
    return AsyncMock()


# ---------------------------------------------------------------------------
# owner_only decorator
# ---------------------------------------------------------------------------

class TestOwnerOnly:
    @pytest.mark.asyncio
    async def test_authorized_user_executes(self, mock_platform):
        called = False

        @owner_only
        async def dummy(platform, msg, msg_ref):
            nonlocal called
            called = True

        msg = make_message(user_id=12345)
        await dummy(mock_platform, msg, AsyncMock())

        assert called

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self, mock_platform):
        called = False

        @owner_only
        async def dummy(platform, msg, msg_ref):
            nonlocal called
            called = True

        mock_platform.is_owner = MagicMock(return_value=False)
        msg = make_message(user_id=99999)
        ref = AsyncMock()
        await dummy(mock_platform, msg, ref)

        assert not called
        mock_platform.reply.assert_awaited_once_with(ref, STRINGS["unauthorized"])


# ---------------------------------------------------------------------------
# make_handlers keys
# ---------------------------------------------------------------------------

class TestMakeHandlers:
    def test_returns_correct_keys(self, handlers):
        expected = {
            "start", "help", "workspaces", "specialists", "status",
            "reset", "focus", "ping", "checkupdate", "doupdate",
            "voice", "message",
            # Spec 006: [GET_EVENTS] handler always exposed (no collab dep).
            "_handle_get_events",
        }
        assert set(handlers.keys()) == expected


# ---------------------------------------------------------------------------
# cmd_help
# ---------------------------------------------------------------------------

class TestCmdHelp:
    @pytest.mark.asyncio
    async def test_help_no_focus(self, handlers, mock_platform, msg_ref):
        msg = make_message()
        await handlers["help"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once()
        text = mock_platform.reply.call_args[0][1]
        assert STRINGS["help_text"] in text
        assert "Focus active" not in text

    @pytest.mark.asyncio
    async def test_help_with_focus(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("myws", "/tmp", "Test workspace", thread_id=100)
        agent_manager.set_focus("myws")

        msg = make_message()
        await handlers["help"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "Focus active" in text
        assert "myws" in text


# ---------------------------------------------------------------------------
# cmd_workspaces
# ---------------------------------------------------------------------------

class TestCmdWorkspaces:
    @pytest.mark.asyncio
    async def test_no_workspaces(self, handlers, mock_platform, msg_ref):
        msg = make_message()
        await handlers["workspaces"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, STRINGS["no_workspaces"])

    @pytest.mark.asyncio
    async def test_with_workspaces(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("alpha", "/tmp/a", "Alpha workspace", thread_id=10)
        agent_manager.add_agent("beta", "/tmp/b", "Beta workspace", thread_id=11)
        agent_manager.set_focus("alpha")

        msg = make_message()
        await handlers["workspaces"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "alpha" in text
        assert "beta" in text
        assert STRINGS["workspaces_title"] in text
        # Focus marker present for alpha
        assert " *" in text


# ---------------------------------------------------------------------------
# cmd_specialists
# ---------------------------------------------------------------------------

class TestCmdSpecialists:
    @pytest.mark.asyncio
    async def test_no_specialists(self, handlers, mock_platform, msg_ref):
        msg = make_message()
        await handlers["specialists"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, STRINGS["no_specialists"])

    @pytest.mark.asyncio
    async def test_with_specialists(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent(
            "reviewer", "/tmp/r", "Code reviewer",
            agent_type="specialist", thread_id=20,
        )

        msg = make_message()
        await handlers["specialists"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "reviewer" in text
        assert STRINGS["specialists_title"] in text


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_status_output(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("ws1", "/tmp/ws1", "Workspace one", thread_id=10)
        agent_manager.set_focus("ws1")

        msg = make_message()
        await handlers["status"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "Robyx Status" in text
        assert "agents" in text
        assert "focus: ws1" in text


# ---------------------------------------------------------------------------
# cmd_reset
# ---------------------------------------------------------------------------

class TestCmdReset:
    @pytest.mark.asyncio
    async def test_no_args(self, handlers, mock_platform, msg_ref):
        msg = make_message(args=[])
        await handlers["reset"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, "Usage: /reset <name>")

    @pytest.mark.asyncio
    async def test_valid_agent(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("alpha", "/tmp/a", "A workspace", thread_id=10)
        agent = agent_manager.get("alpha")
        old_session = agent.session_id
        agent.message_count = 5
        agent.session_started = True

        msg = make_message(args=["alpha"])
        await handlers["reset"](mock_platform, msg, msg_ref)

        assert agent.session_id != old_session
        assert agent.message_count == 0
        assert agent.session_started is False
        text = mock_platform.reply.call_args[0][1]
        assert "alpha" in text

    @pytest.mark.asyncio
    async def test_unknown_agent(self, handlers, mock_platform, msg_ref):
        msg = make_message(args=["nonexistent"])
        await handlers["reset"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "nonexistent" in text
        assert "not found" in text


# ---------------------------------------------------------------------------
# cmd_focus
# ---------------------------------------------------------------------------

class TestCmdFocus:
    @pytest.mark.asyncio
    async def test_no_args_no_focus(self, handlers, mock_platform, msg_ref):
        msg = make_message(args=[])
        await handlers["focus"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, STRINGS["focus_none"])

    @pytest.mark.asyncio
    async def test_no_args_focus_active(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("ws1", "/tmp", "WS1", thread_id=10)
        agent_manager.set_focus("ws1")

        msg = make_message(args=[])
        await handlers["focus"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "ws1" in text

    @pytest.mark.asyncio
    async def test_focus_off(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("ws1", "/tmp", "WS1", thread_id=10)
        agent_manager.set_focus("ws1")

        msg = make_message(args=["off"])
        await handlers["focus"](mock_platform, msg, msg_ref)

        assert agent_manager.focused_agent is None
        text = mock_platform.reply.call_args[0][1]
        assert "ws1" in text  # was: ws1

    @pytest.mark.asyncio
    async def test_focus_off_no_previous(self, handlers, mock_platform, msg_ref, agent_manager):
        msg = make_message(args=["off"])
        await handlers["focus"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "Focus off" in text

    @pytest.mark.asyncio
    async def test_focus_valid_agent(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("ws1", "/tmp", "WS1", thread_id=10)

        msg = make_message(args=["ws1"])
        await handlers["focus"](mock_platform, msg, msg_ref)

        assert agent_manager.focused_agent == "ws1"
        text = mock_platform.reply.call_args[0][1]
        assert "ws1" in text

    @pytest.mark.asyncio
    async def test_focus_unknown_agent(self, handlers, mock_platform, msg_ref):
        msg = make_message(args=["ghost"])
        await handlers["focus"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "ghost" in text
        assert "not found" in text


# ---------------------------------------------------------------------------
# cmd_ping
# ---------------------------------------------------------------------------

class TestCmdPing:
    @pytest.mark.asyncio
    async def test_ping_response(self, handlers, mock_platform, msg_ref, agent_manager):
        agent_manager.add_agent("ws1", "/tmp", "WS1", thread_id=10)
        agent_manager.set_focus("ws1")

        msg = make_message()
        await handlers["ping"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "alive" in text
        assert "focus: ws1" in text

    @pytest.mark.asyncio
    async def test_ping_no_focus(self, handlers, mock_platform, msg_ref):
        msg = make_message()
        await handlers["ping"](mock_platform, msg, msg_ref)

        text = mock_platform.reply.call_args[0][1]
        assert "alive" in text
        assert "focus" not in text


# ---------------------------------------------------------------------------
# handle_message
# ---------------------------------------------------------------------------

class TestHandleMessage:
    @pytest.mark.asyncio
    @patch("handlers.restart_service")
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    async def test_direct_config_update_bypasses_ai_and_restarts(
        self, mock_invoke, mock_restart, handlers, mock_platform, msg_ref, tmp_path
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("AI_BACKEND=claude\n")

        with patch("handlers._config.PROJECT_ROOT", tmp_path):
            msg = make_message(text="OPENAI_API_KEY=sk-test")
            await handlers["message"](mock_platform, msg, msg_ref)

        mock_invoke.assert_not_awaited()
        mock_restart.assert_called_once()
        assert "OPENAI_API_KEY=sk-test" in env_file.read_text()
        mock_platform.reply.assert_awaited_once()
        reply_text = mock_platform.reply.call_args.args[1]
        assert "OPENAI_API_KEY" in reply_text
        assert "sk-test" not in reply_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="AI says hi")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["AI says hi"])
    async def test_routes_to_robyx_by_default(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="hello there")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_invoke.assert_awaited_once()
        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "robyx"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="workspace reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["workspace reply"])
    async def test_routes_to_workspace_in_topic(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("alpha", "/tmp/a", "Alpha WS", thread_id=42)

        msg = make_message(text="do something", thread_id=42)
        await handlers["message"](mock_platform, msg, msg_ref)

        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "alpha"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="workspace reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["workspace reply"])
    async def test_routes_to_workspace_by_chat_id_when_platform_uses_top_level_channels(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("alpha", "/tmp/a", "Alpha WS", thread_id="C01WORK")
        mock_platform.is_main_thread = MagicMock(return_value=False)

        msg = make_message(text="do something")
        msg.chat_id = "C01WORK"
        msg.thread_id = None
        await handlers["message"](mock_platform, msg, msg_ref)

        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "alpha"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="mention reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["mention reply"])
    async def test_routes_to_mentioned_agent(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("beta", "/tmp/b", "Beta WS", thread_id=50)

        msg = make_message(text="@beta please check this")
        await handlers["message"](mock_platform, msg, msg_ref)

        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "beta"

    @pytest.mark.asyncio
    async def test_empty_message_after_stripping(self, handlers, mock_platform, msg_ref):
        msg = make_message(text="   ")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, STRINGS["empty_message"])

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="robyx reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["robyx reply"])
    async def test_unmapped_topic_replies_with_hint_and_no_ai(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        # User writes in a topic that is NOT registered with any agent.
        # Old behaviour: fall through to Robyx and silently redirect to main
        # thread — so the user saw typing appear in #general. New behaviour:
        # stay in the originating topic, reply with a hint, do NOT invoke AI.
        msg = make_message(text="hello robyx", thread_id=99)
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_invoke.assert_not_awaited()
        mock_platform.reply.assert_awaited_once_with(
            msg_ref,
            STRINGS["unmapped_topic"],
            parse_mode="markdown",
        )

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="robyx reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["robyx reply"])
    async def test_main_thread_preserves_thread_id(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        # User writes in the platform's main destination (Telegram general,
        # thread_id=None per the default mock_platform.is_main_thread lambda).
        # Robyx must respond there — thread_id is preserved, no redirect.
        msg = make_message(text="hello robyx", thread_id=None)
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_invoke.assert_awaited_once()
        call_kwargs = mock_invoke.call_args[1]
        assert call_kwargs.get("thread_id") is None

    @pytest.mark.asyncio
    async def test_null_text_returns_early(self, handlers, mock_platform, msg_ref):
        msg = make_message(text=None)
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_not_awaited()

    # ─────────────────────────────────────────────────────────────────
    # Regression coverage for the v0.20.16 typing-latency bug:
    # "in Headquarters the typing indicator does not appear immediately
    # when I send a message."
    #
    # Three contracts must hold:
    #   1. ``send_typing`` is invoked for messages that land in the
    #      General topic / Headquarters (thread_id=None) — same as for
    #      forum topics. This exercises the routing for Telegram's
    #      main destination, which earlier regressions had silently
    #      broken by gating typing behind a forum-topic check.
    #   2. The typing call does NOT block the rest of the handler — it
    #      runs as a background task so the agent invocation can start
    #      in parallel with Telegram's roundtrip. This rules out the
    #      class of bugs where a slow ``send_typing`` (cold TLS
    #      handshake, network blip) postpones the entire message
    #      processing pipeline.
    #   3. A failing ``send_typing`` is logged at WARNING, not silently
    #      swallowed — silent failures were how the previous bug
    #      survived multiple "fixed" releases.
    # ─────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="ok")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["ok"])
    async def test_typing_fires_for_headquarters_general_topic(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        """Headquarters = Telegram's General topic, addressed with
        ``thread_id=None``. Typing must be sent with ``thread_id=None``
        so Telegram displays it in General (NOT routed to a forum
        topic). This is the exact case the user reported."""
        msg = make_message(text="ciao", thread_id=None)
        await handlers["message"](mock_platform, msg, msg_ref)
        # Yield the loop once so the create_task'd typing send actually
        # runs before we make assertions.
        import asyncio as _aio
        await _aio.sleep(0)

        mock_platform.send_typing.assert_awaited()
        # Must be addressed at the chat with no thread_id — that's how
        # Telegram routes typing to General in a forum supergroup.
        call_args = mock_platform.send_typing.await_args
        assert call_args.args[0] == msg.chat_id
        # Either positional or kw — the contract is "thread_id is None".
        thread_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("thread_id")
        assert thread_arg is None

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="ok")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["ok"])
    async def test_typing_does_not_block_message_processing(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        """If ``send_typing`` is slow (cold TLS handshake, network blip
        post-wake), the agent invocation must NOT wait for it. The
        early send_typing is a background task; the handler proceeds to
        ``invoke_ai`` immediately."""
        import asyncio as _aio

        typing_started = _aio.Event()
        typing_can_finish = _aio.Event()

        async def slow_typing(chat_id, thread_id=None):
            typing_started.set()
            # Block until the test releases us — simulating a slow
            # network call.
            await typing_can_finish.wait()

        mock_platform.send_typing = AsyncMock(side_effect=slow_typing)

        msg = make_message(text="ciao", thread_id=None)
        # Run the handler with a short timeout — if it blocks waiting
        # on ``send_typing`` we'll hit the timeout and fail clearly.
        async def _run():
            await handlers["message"](mock_platform, msg, msg_ref)

        handler_task = _aio.create_task(_run())
        # Give the handler a tick to spawn the typing task and proceed.
        await _aio.sleep(0)
        # The typing call should have started but the handler should
        # already have moved past it and called invoke_ai.
        await _aio.wait_for(handler_task, timeout=2)
        mock_invoke.assert_awaited_once()
        # Now release the slow typing so the background task completes
        # cleanly (otherwise pytest reports an unawaited coroutine).
        typing_can_finish.set()
        await _aio.sleep(0)

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="ok")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["ok"])
    async def test_typing_failure_is_logged_not_silenced(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, caplog,
    ):
        """A bare ``except Exception: pass`` was hiding send_typing
        failures, which is why the original bug slipped past every
        release that claimed to fix it. Failures must surface in the
        log so we can diagnose them."""
        import asyncio as _aio
        import logging

        mock_platform.send_typing = AsyncMock(side_effect=RuntimeError("network down"))

        msg = make_message(text="ciao", thread_id=None)
        with caplog.at_level(logging.WARNING):
            await handlers["message"](mock_platform, msg, msg_ref)
            await _aio.sleep(0)  # let the background task run

        assert any(
            "Early typing send failed" in record.message
            and "network down" in record.message
            for record in caplog.records
        ), "send_typing failure must be logged at WARNING, not silenced"


# ---------------------------------------------------------------------------
# _resolve_from_context (tested via handle_message behavior)
# ---------------------------------------------------------------------------

class TestResolveFromContext:
    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["reply"])
    async def test_known_topic_routes_to_agent(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("ws1", "/tmp/ws1", "Workspace 1", thread_id=77)

        msg = make_message(text="check status", thread_id=77)
        await handlers["message"](mock_platform, msg, msg_ref)

        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "ws1"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["reply"])
    async def test_general_thread_falls_through_to_resolve(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        msg = make_message(text="hello", thread_id=None)
        await handlers["message"](mock_platform, msg, msg_ref)

        agent_arg = mock_invoke.call_args[0][0]
        assert agent_arg.name == "robyx"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["reply"])
    async def test_unknown_topic_does_not_invoke_ai(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        # Unmapped topic: the router must refuse to invoke the AI and must
        # reply in the same thread instead of silently migrating the
        # conversation to #general.
        msg = make_message(text="hello", thread_id=999)
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_invoke.assert_not_awaited()
        mock_platform.reply.assert_awaited_once_with(
            msg_ref,
            STRINGS["unmapped_topic"],
            parse_mode="markdown",
        )


# ---------------------------------------------------------------------------
# _send_response (tested via handle_message integration)
# ---------------------------------------------------------------------------

class TestSendResponse:
    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="hi from robyx")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["hi from robyx"])
    async def test_robyx_tag(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="hello")
        await handlers["message"](mock_platform, msg, msg_ref)

        call_kwargs = mock_platform.send_message.call_args[1]
        assert "*Robyx*" in call_kwargs["text"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="specialist reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["specialist reply"])
    async def test_specialist_tag(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent(
            "reviewer", "/tmp/r", "Code reviewer",
            agent_type="specialist", thread_id=20,
        )

        msg = make_message(text="@reviewer check code")
        await handlers["message"](mock_platform, msg, msg_ref)

        call_kwargs = mock_platform.send_message.call_args[1]
        assert "*reviewer* [specialist]" in call_kwargs["text"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="ws reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["ws reply"])
    async def test_workspace_tag(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("alpha", "/tmp/a", "Alpha WS", thread_id=42)

        msg = make_message(text="do stuff", thread_id=42)
        await handlers["message"](mock_platform, msg, msg_ref)

        call_kwargs = mock_platform.send_message.call_args[1]
        assert "*alpha*" in call_kwargs["text"]
        assert "[specialist]" not in call_kwargs["text"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="ws reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["ws reply"])
    async def test_with_thread_id(
        self, mock_split, mock_spec, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        agent_manager.add_agent("alpha", "/tmp/a", "Alpha WS", thread_id=42)

        msg = make_message(text="do stuff", thread_id=42)
        await handlers["message"](mock_platform, msg, msg_ref)

        call_kwargs = mock_platform.send_message.call_args[1]
        assert call_kwargs["thread_id"] == 42

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="bad *markdown")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", return_value=["bad *markdown"])
    async def test_markdown_fallback_to_plain(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        # First send_message raises (markdown parse error), second succeeds
        mock_platform.send_message = AsyncMock(
            side_effect=[Exception("Can't parse entities"), None]
        )

        msg = make_message(text="hello")
        await handlers["message"](mock_platform, msg, msg_ref)

        assert mock_platform.send_message.call_count == 2
        # Second call should NOT have parse_mode (plain text fallback)
        fallback_kwargs = mock_platform.send_message.call_args_list[1][1]
        assert "parse_mode" not in fallback_kwargs
        assert "[robyx]" in fallback_kwargs["text"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="x" * 5000)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message")
    async def test_split_message_receives_platform_max_length(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        """Regression: Discord caps messages at 2000 chars. The handler
        must pass ``platform.max_message_length`` to ``split_message``
        instead of letting it default to Telegram's 4000."""
        mock_split.return_value = ["chunk"]
        mock_platform.max_message_length = 2000  # Discord ceiling

        msg = make_message(text="produce a long reply")
        await handlers["message"](mock_platform, msg, msg_ref)

        assert mock_split.called
        max_len = mock_split.call_args.kwargs.get("max_len")
        # Tag prefix is reserved (~64 chars), so the budget must be
        # platform_cap - prefix_margin and strictly under the cap.
        assert max_len is not None
        assert max_len < 2000, "split must respect platform cap"
        assert max_len >= 1900, "but should consume nearly the whole budget"


# ---------------------------------------------------------------------------
# Helpers for new tests
# ---------------------------------------------------------------------------

def _make_msg_and_ref(mock_platform):
    """Return (msg, msg_ref) where msg_ref is returned by platform.reply."""
    msg = make_message()
    sent_ref = AsyncMock()
    mock_platform.reply = AsyncMock(return_value=sent_ref)
    return msg, sent_ref


# ---------------------------------------------------------------------------
# cmd_checkupdate
# ---------------------------------------------------------------------------

class TestCmdCheckUpdate:
    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.check_for_updates", return_value=None)
    async def test_checkupdate_no_update(self, mock_check, mock_ver, handlers, mock_platform):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["checkupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "latest version" in text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.check_for_updates", return_value={
        "status": "available",
        "current": "0.1.0",
        "version": "0.2.0",
        "release_notes": {"body": "Bug fixes and improvements"},
    })
    async def test_checkupdate_available(self, mock_check, mock_ver, handlers, mock_platform):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["checkupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "0.2.0" in text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.check_for_updates", return_value={
        "status": "breaking",
        "current": "0.1.0",
        "version": "1.0.0",
        "release_notes": {"body": "Breaking changes"},
    })
    async def test_checkupdate_breaking(self, mock_check, mock_ver, handlers, mock_platform):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["checkupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "1.0.0" in text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.check_for_updates", return_value={
        "status": "incompatible",
        "current": "0.1.0",
        "version": "2.0.0",
        "release_notes": {"body": "Incompatible", "min_compatible": "1.0.0"},
    })
    async def test_checkupdate_incompatible(self, mock_check, mock_ver, handlers, mock_platform):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["checkupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "2.0.0" in text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.check_for_updates", side_effect=Exception("network error"))
    async def test_checkupdate_exception(self, mock_check, mock_ver, handlers, mock_platform):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["checkupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "network error" in text


# ---------------------------------------------------------------------------
# cmd_doupdate
# ---------------------------------------------------------------------------

class TestCmdDoUpdate:
    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.get_pending_update", return_value=None)
    @patch("scheduler.parse_tasks", return_value=[])
    @patch("scheduler.check_lock", return_value=(False, 0))
    async def test_doupdate_no_pending(
        self, mock_lock, mock_tasks, mock_pending, mock_ver, handlers, mock_platform
    ):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited()
        last_text = mock_platform.edit_message.call_args[0][1]
        assert "No pending update" in last_text

    @pytest.mark.asyncio
    @patch("scheduler.parse_tasks", return_value=[])
    @patch("scheduler.check_lock", return_value=(False, 0))
    async def test_doupdate_busy_agents_blocked(
        self, mock_lock, mock_tasks, handlers, mock_platform, agent_manager
    ):
        agent_manager.add_agent("ws1", "/tmp/ws1", "WS1", thread_id=10)
        agent = agent_manager.get("ws1")
        agent.busy = True

        msg = make_message(args=[])
        mock_platform.reply = AsyncMock(return_value=AsyncMock())
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited_once()
        text = mock_platform.edit_message.call_args[0][1]
        assert "blocked" in text.lower() or "busy" in text.lower()

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.get_pending_update", return_value={"version": "0.2.0", "current": "0.1.0"})
    @patch("handlers.apply_update", new_callable=AsyncMock, return_value=(True, "0.2.0"))
    @patch("handlers.restart_service")
    @patch("scheduler.parse_tasks", return_value=[])
    @patch("scheduler.check_lock", return_value=(False, 0))
    async def test_doupdate_busy_agents_force(
        self, mock_lock, mock_tasks, mock_restart, mock_apply,
        mock_pending, mock_ver, handlers, mock_platform, agent_manager
    ):
        agent_manager.add_agent("ws1", "/tmp/ws1", "WS1", thread_id=10)
        agent = agent_manager.get("ws1")
        agent.busy = True

        msg = make_message(args=["force"])
        mock_platform.reply = AsyncMock(return_value=AsyncMock())
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        # Should proceed past busy check
        mock_apply.assert_awaited_once()
        mock_restart.assert_called_once()

    @pytest.mark.asyncio
    @patch("scheduler.get_running_tasks", return_value=[{"name": "backup", "_pid": 1234}])
    async def test_doupdate_running_tasks_blocked(
        self, mock_running, handlers, mock_platform
    ):
        msg = make_message(args=[])
        mock_platform.reply = AsyncMock(return_value=AsyncMock())
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited()
        text = mock_platform.edit_message.call_args[0][1]
        assert "blocked" in text.lower() or "running" in text.lower()

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.get_pending_update", return_value={"version": "0.2.0", "current": "0.1.0"})
    @patch("handlers.apply_update", new_callable=AsyncMock, return_value=(True, "0.2.0"))
    @patch("handlers.restart_service")
    @patch("scheduler.get_running_tasks", return_value=[])
    async def test_doupdate_success(
        self, mock_running, mock_restart, mock_apply,
        mock_pending, mock_ver, handlers, mock_platform
    ):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_apply.assert_awaited_once()
        mock_restart.assert_called_once()
        # Last edit_message should contain success message
        last_text = mock_platform.edit_message.call_args[0][1]
        assert "0.2.0" in last_text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.get_pending_update", return_value={"version": "0.2.0", "current": "0.1.0"})
    @patch("handlers.apply_update", new_callable=AsyncMock, return_value=(False, "git conflict"))
    @patch("handlers.restart_service")
    @patch("scheduler.parse_tasks", return_value=[])
    @patch("scheduler.check_lock", return_value=(False, 0))
    async def test_doupdate_failure(
        self, mock_lock, mock_tasks, mock_restart, mock_apply,
        mock_pending, mock_ver, handlers, mock_platform
    ):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_restart.assert_not_called()
        last_text = mock_platform.edit_message.call_args[0][1]
        assert "git conflict" in last_text

    @pytest.mark.asyncio
    @patch("handlers.get_current_version", return_value="0.1.0")
    @patch("handlers.get_pending_update", side_effect=Exception("disk error"))
    @patch("scheduler.parse_tasks", return_value=[])
    @patch("scheduler.check_lock", return_value=(False, 0))
    async def test_doupdate_get_pending_exception(
        self, mock_lock, mock_tasks, mock_pending, mock_ver, handlers, mock_platform
    ):
        msg, sent_ref = _make_msg_and_ref(mock_platform)
        await handlers["doupdate"](mock_platform, msg, AsyncMock())

        mock_platform.edit_message.assert_awaited()
        last_text = mock_platform.edit_message.call_args[0][1]
        assert "disk error" in last_text


# ---------------------------------------------------------------------------
# _process_and_send exception handling
# ---------------------------------------------------------------------------

class TestProcessAndSendException:
    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, side_effect=Exception("AI crashed"))
    async def test_process_and_send_exception(self, mock_invoke, handlers, mock_platform, msg_ref, agent_manager):
        """When invoke_ai raises, an error message should be sent."""
        msg = make_message(text="hello")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.send_message.assert_awaited()
        text = mock_platform.send_message.call_args[1]["text"]
        assert "AI crashed" in text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, side_effect=Exception("AI crashed"))
    async def test_process_and_send_exception_send_fails(
        self, mock_invoke, handlers, mock_platform, msg_ref, agent_manager
    ):
        """When invoke_ai raises AND the error send fails, it should not propagate."""
        mock_platform.send_message = AsyncMock(side_effect=Exception("Telegram down"))

        msg = make_message(text="hello")
        # Should not raise
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.send_message.assert_awaited()

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, side_effect=Exception("boom_with_*markdown*"))
    async def test_process_and_send_exception_markdown_fallback(
        self, mock_invoke, handlers, mock_platform, msg_ref, agent_manager
    ):
        """If the markdown-formatted error send fails, _safe_send must retry
        with plain text so the user always gets visibility into the failure.
        """
        calls = []

        async def flaky_send(**kwargs):
            calls.append(kwargs)
            if kwargs.get("parse_mode") == "markdown":
                raise Exception("markdown parse error")
            return None

        mock_platform.send_message = AsyncMock(side_effect=flaky_send)

        msg = make_message(text="hello")
        await handlers["message"](mock_platform, msg, msg_ref)

        # Two send attempts: one markdown (failed), one plain (succeeded).
        assert len(calls) == 2
        assert calls[0].get("parse_mode") == "markdown"
        assert "parse_mode" not in calls[1]
        assert "boom_with_*markdown*" in calls[1]["text"]


# ---------------------------------------------------------------------------
# [RESTART] pattern handling
# ---------------------------------------------------------------------------


class TestRestartPattern:
    @pytest.mark.asyncio
    @patch("handlers.restart_service")
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="Config aggiornata.\n[RESTART]")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_restart_triggered(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_restart,
        handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="set OPENAI_API_KEY=sk-test")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_restart.assert_called_once()
        # The [RESTART] tag should be stripped from the response
        sent_text = mock_platform.send_message.call_args_list[0][1]["text"]
        assert "[RESTART]" not in sent_text

    @pytest.mark.asyncio
    @patch("handlers.restart_service")
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="No restart needed.")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_no_restart_without_pattern(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_restart,
        handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="hello")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_restart.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_workspace_commands via handle_message
# ---------------------------------------------------------------------------

class TestHandleWorkspaceCommands:
    @pytest.mark.asyncio
    @patch("handlers.create_workspace", new_callable=AsyncMock)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_single_workspace(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_WORKSPACE name="test" type="interactive" frequency="none" '
            'model="sonnet" scheduled_at="none"]'
            '[AGENT_INSTRUCTIONS]Do stuff[/AGENT_INSTRUCTIONS]'
        )
        mock_create.return_value = {"display_name": "test", "thread_id": 42}

        msg = make_message(text="create a workspace")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_create.assert_awaited_once()
        # Response should mention workspace created
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "test" in sent_text
        assert "42" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.create_workspace", new_callable=AsyncMock)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_multiple_workspaces_progress(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_WORKSPACE name="ws1" type="interactive" frequency="none" '
            'model="sonnet" scheduled_at="none"]'
            '[AGENT_INSTRUCTIONS]First[/AGENT_INSTRUCTIONS]'
            '[CREATE_WORKSPACE name="ws2" type="interactive" frequency="none" '
            'model="sonnet" scheduled_at="none"]'
            '[AGENT_INSTRUCTIONS]Second[/AGENT_INSTRUCTIONS]'
        )
        mock_create.side_effect = [
            {"display_name": "ws1", "thread_id": 10},
            {"display_name": "ws2", "thread_id": 11},
        ]

        msg = make_message(text="create two workspaces")
        await handlers["message"](mock_platform, msg, msg_ref)

        assert mock_create.await_count == 2
        # Progress messages sent for multi-workspace creation
        calls = mock_platform.send_message.call_args_list
        progress_texts = [c[1]["text"] for c in calls if "Creating workspace" in c[1].get("text", "")]
        assert len(progress_texts) == 2

    @pytest.mark.asyncio
    @patch("handlers.create_workspace", new_callable=AsyncMock, return_value=None)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_workspace_fails(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_WORKSPACE name="bad" type="interactive" frequency="none" '
            'model="sonnet" scheduled_at="none"]'
            '[AGENT_INSTRUCTIONS]Nope[/AGENT_INSTRUCTIONS]'
        )

        msg = make_message(text="create workspace bad")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Failed" in sent_text

    @pytest.mark.asyncio
    @patch(
        "handlers.create_workspace",
        new_callable=AsyncMock,
        side_effect=ValueError("one-shot workspaces require scheduled_at"),
    )
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_workspace_rejection_reason_is_shown(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_WORKSPACE name="later" type="one-shot" frequency="none" '
            'model="sonnet" scheduled_at="none"]'
            '[AGENT_INSTRUCTIONS]Nope[/AGENT_INSTRUCTIONS]'
        )

        msg = make_message(text="create workspace later")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "not created" in sent_text
        assert "scheduled_at" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.close_workspace", new_callable=AsyncMock, return_value=True)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_close_workspace_success(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_close,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = '[CLOSE_WORKSPACE name="myws"]'

        msg = make_message(text="close workspace myws")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_close.assert_awaited_once_with("myws", unittest.mock.ANY, platform=unittest.mock.ANY)
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "closed" in sent_text.lower()

    @pytest.mark.asyncio
    @patch("handlers.close_workspace", new_callable=AsyncMock, return_value=False)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_close_workspace_not_found(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_close,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = '[CLOSE_WORKSPACE name="ghost"]'

        msg = make_message(text="close workspace ghost")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "not found" in sent_text.lower()

    @pytest.mark.asyncio
    @patch("handlers.create_specialist", new_callable=AsyncMock)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_specialist_success(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create_spec,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_SPECIALIST name="reviewer" model="sonnet"]'
            '[SPECIALIST_INSTRUCTIONS]Review code[/SPECIALIST_INSTRUCTIONS]'
        )
        mock_create_spec.return_value = {"display_name": "reviewer", "thread_id": 55}

        msg = make_message(text="create a specialist")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_create_spec.assert_awaited_once()
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "reviewer" in sent_text
        assert "55" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.create_specialist", new_callable=AsyncMock, return_value=None)
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_create_specialist_fails(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_create_spec,
        handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[CREATE_SPECIALIST name="bad" model="sonnet"]'
            '[SPECIALIST_INSTRUCTIONS]Nope[/SPECIALIST_INSTRUCTIONS]'
        )

        msg = make_message(text="create specialist bad")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Failed" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="Just a plain reply")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_no_patterns(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="just chat")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Just a plain reply" in sent_text


# ---------------------------------------------------------------------------
# [SEND_IMAGE] pattern handling
# ---------------------------------------------------------------------------

class TestHandleSendImage:
    """The SEND_IMAGE pattern must only be activated by explicit agent emission.
    These tests simulate the agent returning such a response and verify that
    the platform's send_photo is called with the parsed arguments and that
    the pattern is stripped from the text the user eventually sees."""

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_single_image_sent_and_stripped(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            'Here is the result.\n'
            '[SEND_IMAGE path="/tmp/foo.png" caption="iter-154 A"]\n'
            'Let me know if you need anything else.'
        )

        msg = make_message(text="mostrami il risultato")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.send_photo.assert_awaited_once()
        kwargs = mock_platform.send_photo.call_args[1]
        assert kwargs["path"] == "/tmp/foo.png"
        assert kwargs["caption"] == "iter-154 A"

        # The SEND_IMAGE line must not leak into the text sent to the user.
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[SEND_IMAGE" not in sent_text
        assert "Here is the result." in sent_text
        assert "Let me know if you need anything else." in sent_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_image_without_caption(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = '[SEND_IMAGE path="/tmp/bar.jpg"]'

        msg = make_message(text="dammi l'immagine")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.send_photo.assert_awaited_once()
        kwargs = mock_platform.send_photo.call_args[1]
        assert kwargs["path"] == "/tmp/bar.jpg"
        assert kwargs["caption"] is None

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_multiple_images_sent_in_order(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        mock_invoke.return_value = (
            '[SEND_IMAGE path="/tmp/a.png" caption="A"]\n'
            '[SEND_IMAGE path="/tmp/b.png" caption="B"]\n'
            '[SEND_IMAGE path="/tmp/c.png" caption="C"]'
        )

        msg = make_message(text="mandami A, B e C")
        await handlers["message"](mock_platform, msg, msg_ref)

        assert mock_platform.send_photo.await_count == 3
        paths = [c[1]["path"] for c in mock_platform.send_photo.call_args_list]
        captions = [c[1]["caption"] for c in mock_platform.send_photo.call_args_list]
        assert paths == ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]
        assert captions == ["A", "B", "C"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_platform_returns_none_appends_error_to_reply(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        """If the adapter logs the failure and returns None (e.g. file too
        large to compress), the user must still see *something* explaining
        that the image was not delivered — never a silent drop."""
        mock_platform.send_photo = AsyncMock(return_value=None)
        mock_invoke.return_value = (
            'Ecco il risultato.\n'
            '[SEND_IMAGE path="/tmp/missing.png" caption="broken"]'
        )

        msg = make_message(text="dammi il risultato")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[SEND_IMAGE" not in sent_text
        assert "Ecco il risultato." in sent_text
        assert "/tmp/missing.png" in sent_text
        assert "Failed" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_platform_raises_does_not_crash_handler(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        mock_platform.send_photo = AsyncMock(side_effect=RuntimeError("boom"))
        mock_invoke.return_value = '[SEND_IMAGE path="/tmp/x.png"]'

        msg = make_message(text="mandami x")
        # Must not raise — the error must be caught and reported inline.
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "/tmp/x.png" in sent_text
        assert "boom" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="Just a reply, no media here.")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_no_pattern_does_not_call_send_photo(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, handlers, mock_platform, msg_ref
    ):
        msg = make_message(text="ciao")
        await handlers["message"](mock_platform, msg, msg_ref)

        mock_platform.send_photo.assert_not_awaited()


# ---------------------------------------------------------------------------
# [REMIND] pattern handling
# ---------------------------------------------------------------------------

class TestHandleRemind:
    """The REMIND pattern is the universal scheduling skill: any agent can
    emit it, the bot translates it into a queue.json entry, and the
    Python reminder engine fires it at the exact time. The pattern must
    never leak into the user-visible reply, and validation failures must
    surface as inline notices (not silent drops)."""

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_in_duration_queues_reminder_and_strips_pattern(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        mock_invoke.return_value = (
            'Ho impostato il reminder.\n'
            '[REMIND in="2m" text="⏰ sei un figo"]'
        )

        msg = make_message(text="ricordami tra 2 minuti che sono un figo")
        await handlers["message"](mock_platform, msg, msg_ref)

        # Pattern stripped from user-visible reply.
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[REMIND" not in sent_text
        assert "Ho impostato il reminder." in sent_text

        # Reminder appended to data/queue.json.
        reminders_file = tmp_path / "data" / "queue.json"
        assert reminders_file.exists()
        data = json.loads(reminders_file.read_text())
        assert len(data) == 1
        entry = data[0]
        assert entry["status"] == "pending"
        assert entry["message"] == "⏰ sei un figo"
        assert entry["id"].startswith("r-")
        assert entry["fire_at"]  # populated

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_at_iso_datetime_with_offset(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        mock_invoke.return_value = (
            '[REMIND at="2099-01-01T09:00:00+02:00" text="capodanno"]'
        )

        msg = make_message(text="ricordami capodanno 2099")
        await handlers["message"](mock_platform, msg, msg_ref)

        data = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert len(data) == 1
        # fire_at is normalised to UTC ISO with offset.
        assert "2099-01-01T07:00:00" in data[0]["fire_at"]
        assert data[0]["message"] == "capodanno"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_multiple_reminders_in_one_response(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        mock_invoke.return_value = (
            'Setting both up.\n'
            '[REMIND in="1h" text="A"]\n'
            '[REMIND in="2h" text="B"]'
        )

        msg = make_message(text="ricordami A tra 1h e B tra 2h")
        await handlers["message"](mock_platform, msg, msg_ref)

        data = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert len(data) == 2
        messages = sorted(e["message"] for e in data)
        assert messages == ["A", "B"]

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_default_thread_is_current_topic(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        """When the agent omits `thread=`, the bot must default to the topic
        the conversation is happening in — the agent never has to know its
        own thread id."""
        from handlers import make_handlers

        agent_manager.add_agent(
            name="assistant",
            work_dir=str(tmp_path / "workspace"),
            description="test assistant",
            agent_type="workspace",
            thread_id=903,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = '[REMIND in="2m" text="hi"]'

        msg = make_message(text="ricordami", thread_id=903)
        await local_handlers["message"](mock_platform, msg, msg_ref)

        data = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert data[0]["thread_id"] == 903
        assert data[0]["chat_id"] == -100999

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_explicit_thread_overrides_default(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        from handlers import make_handlers

        agent_manager.add_agent(
            name="assistant",
            work_dir=str(tmp_path / "workspace"),
            description="test assistant",
            agent_type="workspace",
            thread_id=903,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = '[REMIND in="2m" text="hi" thread="555"]'

        msg = make_message(text="ricordami", thread_id=903)
        await local_handlers["message"](mock_platform, msg, msg_ref)

        data = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert data[0]["thread_id"] == 555
        assert data[0]["chat_id"] == -100999

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_invalid_duration_appends_inline_error(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        mock_invoke.return_value = (
            'Done.\n[REMIND in="forever" text="hi"]'
        )

        msg = make_message(text="ricordami")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[REMIND" not in sent_text
        assert "Reminder rejected" in sent_text
        # No reminder file should be created on a parse-only failure.
        reminders_file = tmp_path / "data" / "queue.json"
        assert not reminders_file.exists() or json.loads(reminders_file.read_text()) == []

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_both_at_and_in_rejected(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        mock_invoke.return_value = (
            '[REMIND at="2099-01-01T09:00:00+00:00" in="2m" text="x"]'
        )

        msg = make_message(text="ricordami")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Reminder rejected" in sent_text

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_missing_text_rejected(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref,
    ):
        mock_invoke.return_value = '[REMIND in="2m" text=""]'

        msg = make_message(text="ricordami")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Reminder rejected" in sent_text
        assert "missing" in sent_text.lower()

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="Just a reply, no scheduling here.")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_no_pattern_does_not_create_file(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        msg = make_message(text="ciao")
        await handlers["message"](mock_platform, msg, msg_ref)

        assert not (tmp_path / "data" / "queue.json").exists()


# ---------------------------------------------------------------------------
# [REMIND agent="..."] action mode — "at T *do* that"
# ---------------------------------------------------------------------------


class TestHandleRemindAction:
    """H3: the REMIND pattern has two modes. Without ``agent=`` it queues a
    plain text reminder (legacy behaviour covered by ``TestHandleRemind``).
    With ``agent="name"`` it schedules a one-shot *execution* of that agent
    at the fire time, routed into the timed task queue. The ``text=`` value
    becomes the agent's prompt.

    The two modes must be strictly disjoint: an action reminder must not
    create ``data/queue.json`` and a plain reminder must not create
    ``data/queue.json``. Validation rejects unknown targets and
    refuses Robyx (orchestrator) as a target before any write."""

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_action_mode_routes_to_timed_queue(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        from handlers import make_handlers

        agent_manager.add_agent(
            name="cleanup",
            work_dir=str(tmp_path / "workspace"),
            description="nightly cleanup",
            agent_type="workspace",
            model="fast",
            thread_id=777,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = (
            'Reminder set.\n'
            '[REMIND at="2099-04-10T09:00:00+02:00" agent="cleanup" '
            'text="Run the daily cleanup and post the summary."]'
        )

        msg = make_message(text="ogni giorno alle 9 fai la pulizia")
        await local_handlers["message"](mock_platform, msg, msg_ref)

        # Pattern stripped from user-visible reply.
        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[REMIND" not in sent_text
        assert "Reminder set." in sent_text

        # Action mode: queue.json created, queue.json untouched.
        tq = tmp_path / "data" / "queue.json"
        assert tq.exists()
        tasks = json.loads(tq.read_text())
        assert len(tasks) == 1
        t = tasks[0]
        assert t["type"] == "one-shot"
        assert t["status"] == "pending"
        assert t["agent_file"] == "agents/cleanup.md"
        assert t["prompt"] == "Run the daily cleanup and post the summary."
        assert t["source"] == "remind"
        # Target agent's own thread is used by default, not the caller's.
        assert t["thread_id"] == "777"
        # scheduled_at normalised to UTC offset.
        assert "2099-04-10T07:00:00" in t["scheduled_at"]

        # In unified queue, action entries coexist with reminder entries.

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_action_targets_specialist_uses_specialists_path(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        """A specialist target must resolve to ``specialists/<name>.md``, not
        ``agents/<name>.md`` — the timed scheduler reads the file from
        ``DATA_DIR/<agent_file>``."""
        from handlers import make_handlers

        agent_manager.add_agent(
            name="reviewer",
            work_dir=str(tmp_path / "workspace"),
            description="code reviewer",
            agent_type="specialist",
            model="powerful",
            thread_id=800,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = (
            '[REMIND in="1h" agent="reviewer" text="Review the latest PR."]'
        )
        msg = make_message(text="tra un'ora fai la review")
        await local_handlers["message"](mock_platform, msg, msg_ref)

        tasks = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert tasks[0]["agent_file"] == "specialists/reviewer.md"
        assert tasks[0]["thread_id"] == "800"
        assert tasks[0]["model"] == "powerful"

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_unknown_agent_rejected_with_inline_error(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        mock_invoke.return_value = (
            '[REMIND in="1h" agent="ghost" text="do stuff"]'
        )
        msg = make_message(text="ricordami")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "[REMIND" not in sent_text
        assert "Reminder rejected" in sent_text
        assert "ghost" in sent_text
        # No side effects: neither queue file should exist.
        assert not (tmp_path / "data" / "queue.json").exists()
        assert not (tmp_path / "data" / "queue.json").exists()

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_robyx_is_not_a_valid_target(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        handlers, mock_platform, msg_ref, tmp_path,
    ):
        """Robyx is the orchestrator — it must not be scheduled as a one-shot
        worker. The default fixture already has ``robyx`` in the manager
        (agent_type='orchestrator'), so this tests the type-guard branch
        rather than the missing-agent branch."""
        mock_invoke.return_value = (
            '[REMIND in="1h" agent="robyx" text="do stuff"]'
        )
        msg = make_message(text="ricordami")
        await handlers["message"](mock_platform, msg, msg_ref)

        sent_text = mock_platform.send_message.call_args[1]["text"]
        assert "Reminder rejected" in sent_text
        assert not (tmp_path / "data" / "queue.json").exists()

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_explicit_thread_overrides_target_default(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        from handlers import make_handlers

        agent_manager.add_agent(
            name="cleanup",
            work_dir=str(tmp_path / "workspace"),
            description="cleanup",
            agent_type="workspace",
            thread_id=777,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = (
            '[REMIND in="1h" agent="cleanup" text="go" thread="42"]'
        )
        msg = make_message(text="ricordami")
        await local_handlers["message"](mock_platform, msg, msg_ref)

        tasks = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert tasks[0]["thread_id"] == "42"  # explicit override wins

    @pytest.mark.asyncio
    @patch("handlers.invoke_ai", new_callable=AsyncMock)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_mixed_text_and_action_in_one_response(
        self, mock_split, mock_deleg, mock_focus, mock_invoke,
        agent_manager, claude_backend, mock_platform, msg_ref, tmp_path,
    ):
        """A single response can emit both a plain reminder and an action
        reminder — the handler must route each to the correct backing store
        without crosstalk."""
        from handlers import make_handlers

        agent_manager.add_agent(
            name="cleanup",
            work_dir=str(tmp_path / "workspace"),
            description="cleanup",
            agent_type="workspace",
            thread_id=777,
        )
        local_handlers = make_handlers(agent_manager, claude_backend)

        mock_invoke.return_value = (
            '[REMIND in="2m" text="buy milk"]\n'
            '[REMIND in="1h" agent="cleanup" text="run cleanup"]'
        )
        msg = make_message(text="ricordami")
        await local_handlers["message"](mock_platform, msg, msg_ref)

        entries = json.loads((tmp_path / "data" / "queue.json").read_text())
        assert len(entries) == 2
        by_type = {}
        for e in entries:
            by_type[e.get("type", "one-shot")] = e
        assert by_type["reminder"]["message"] == "buy milk"
        assert by_type["one-shot"]["prompt"] == "run cleanup"
        assert by_type["one-shot"]["agent_file"] == "agents/cleanup.md"


# ---------------------------------------------------------------------------
# handle_voice
# ---------------------------------------------------------------------------

class TestHandleVoice:
    @pytest.mark.asyncio
    async def test_voice_no_voice_object(self, handlers, mock_platform, msg_ref):
        msg = make_message(voice_file_id=None)
        await handlers["voice"](mock_platform, msg, msg_ref)

        # Should return early, no further action
        mock_platform.send_typing.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("handlers.voice_available", return_value=True)
    @patch("handlers.transcribe_voice", new_callable=AsyncMock, return_value=("hello world", None))
    @patch("handlers.invoke_ai", new_callable=AsyncMock, return_value="AI heard you")
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_delegations", new_callable=AsyncMock, side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda t, **kw: [t])
    async def test_voice_success(
        self, mock_split, mock_deleg, mock_focus, mock_invoke, mock_transcribe, mock_avail,
        handlers, mock_platform, msg_ref
    ):
        msg = make_voice_message()

        with patch("handlers.os.unlink"):
            await handlers["voice"](mock_platform, msg, msg_ref)

        mock_invoke.assert_awaited_once()
        mock_platform.send_message.assert_awaited()

    @pytest.mark.asyncio
    @patch("handlers.voice_available", return_value=True)
    @patch("handlers.transcribe_voice", new_callable=AsyncMock, return_value=(None, "Transcription failed"))
    async def test_voice_error_response(self, mock_transcribe, mock_avail, handlers, mock_platform, msg_ref):
        msg = make_voice_message()

        with patch("handlers.os.unlink"):
            await handlers["voice"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_with(msg_ref, "Transcription failed")

    @pytest.mark.asyncio
    @patch("handlers.voice_available", return_value=True)
    @patch("handlers.transcribe_voice", new_callable=AsyncMock, return_value=("", None))
    async def test_voice_empty_text(self, mock_transcribe, mock_avail, handlers, mock_platform, msg_ref):
        msg = make_voice_message()

        with patch("handlers.os.unlink"):
            await handlers["voice"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_with(msg_ref, STRINGS["ai_empty"])

    @pytest.mark.asyncio
    @patch("handlers.voice_available", return_value=False)
    async def test_voice_not_available(self, mock_avail, handlers, mock_platform, msg_ref):
        msg = make_voice_message()
        await handlers["voice"](mock_platform, msg, msg_ref)

        mock_platform.reply.assert_awaited_once_with(msg_ref, STRINGS["voice_no_key"])
        # Should NOT download the file
        mock_platform.download_voice.assert_not_awaited()


# ---------------------------------------------------------------------------
# Feature 004 — continuous-task macro interception
#
# Contracts under test:
#   - The continuous-task macro is intercepted uniformly for BOTH
#     orchestrator AND workspace-agent executive turns, not just the
#     `is_robyx` branch (the pre-fix routing gap).
#   - The raw macro never reaches the platform send path on ANY fixture:
#     golden, malformed, or realistic-variation.
#   - The confirmation line uses the i18n key, not inlined copy.
#
# The tests patch `handlers.invoke_ai` to return a canned response and
# assert both that `apply_continuous_macros` ran and that the text that
# actually reaches `platform.send_message` contains zero macro tokens.
# ---------------------------------------------------------------------------


class TestContinuousMacroInterception:
    @pytest.mark.asyncio
    @patch("handlers.handle_delegations", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda text, max_len=4000: [text])
    async def test_orchestrator_macro_never_leaks_raw_tokens(
        self, mock_split, mock_focus, mock_deleg,
        handlers, mock_platform, msg_ref, agent_manager, monkeypatch,
    ):
        """Robyx in the main thread emits a golden macro. The user-visible
        text sent to the platform MUST contain none of the macro tokens —
        only the i18n confirmation line."""
        import handlers as handlers_mod

        async def stub_create(**kwargs):
            return {
                "display_name": kwargs["name"],
                "thread_id": 42,
                "branch": "continuous/" + kwargs["name"],
            }

        golden = (
            "Setting up the task.\n\n"
            '[CREATE_CONTINUOUS name="tester" work_dir="%s/w"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective":"x","success_criteria":["y"],'
            '"first_step":{"number":1,"description":"z"}}\n'
            '[/CONTINUOUS_PROGRAM]'
        ) % monkeypatch.setenv  # placeholder, overwritten below

        import config as _cfg
        monkeypatch.setattr(_cfg, "WORKSPACE", handlers_mod.WORKSPACE)
        work = handlers_mod.WORKSPACE / "w"
        work.mkdir(parents=True, exist_ok=True)
        golden = (
            "Setting up the task.\n\n"
            '[CREATE_CONTINUOUS name="tester" work_dir="%s"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective":"x","success_criteria":["y"],'
            '"first_step":{"number":1,"description":"z"}}\n'
            '[/CONTINUOUS_PROGRAM]'
        ) % str(work)

        with patch("handlers.invoke_ai", new_callable=AsyncMock, return_value=golden), \
                patch("continuous_macro._lazy_create_continuous_workspace",
                      return_value=stub_create):
            msg = make_message(text="please set it up", thread_id=None)
            await handlers["message"](mock_platform, msg, msg_ref)

        # Every call to send_message / send_to_channel must have
        # macro-free text.
        for awaited in (
            list(mock_platform.send_message.await_args_list)
            + list(mock_platform.send_to_channel.await_args_list)
            + list(mock_platform.reply.await_args_list)
        ):
            # Find the text argument across the various signatures.
            text_args = [a for a in awaited.args if isinstance(a, str)]
            text_args += [v for k, v in awaited.kwargs.items()
                          if k in ("text", "body") and isinstance(v, str)]
            for t in text_args:
                assert "[CREATE_CONTINUOUS" not in t
                assert "CONTINUOUS_PROGRAM" not in t.upper()

    @pytest.mark.asyncio
    @patch("handlers.handle_specialist_requests", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda text, max_len=4000: [text])
    async def test_workspace_agent_macro_is_intercepted(
        self, mock_split, mock_focus, mock_spec,
        handlers, mock_platform, msg_ref, agent_manager, monkeypatch,
    ):
        """Pre-fix routing gap: a non-robyx workspace agent that emits the
        macro used to leak because `_handle_workspace_commands` was only
        called on the `is_robyx` branch. After the fix, interception runs
        before the is_robyx split, so workspace-agent emissions also get
        stripped."""
        import handlers as handlers_mod

        agent_manager.add_agent(
            "alpha", str(handlers_mod.WORKSPACE / "alpha"),
            "Alpha WS", thread_id=42,
        )

        async def stub_create(**kwargs):
            return {
                "display_name": kwargs["name"],
                "thread_id": 77,
                "branch": "continuous/" + kwargs["name"],
            }

        work = handlers_mod.WORKSPACE / "alpha" / "w"
        work.mkdir(parents=True, exist_ok=True)
        import config as _cfg
        monkeypatch.setattr(_cfg, "WORKSPACE", handlers_mod.WORKSPACE)

        macro_reply = (
            "Kicking off the iterative task.\n\n"
            '[CREATE_CONTINUOUS name="alpha-task" work_dir="%s"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective":"o","success_criteria":["c"],'
            '"first_step":{"number":1,"description":"s"}}\n'
            '[/CONTINUOUS_PROGRAM]'
        ) % str(work)

        with patch("handlers.invoke_ai", new_callable=AsyncMock, return_value=macro_reply), \
                patch("continuous_macro._lazy_create_continuous_workspace",
                      return_value=stub_create):
            msg = make_message(text="please set it up", thread_id=42)
            await handlers["message"](mock_platform, msg, msg_ref)

        for awaited in (
            list(mock_platform.send_message.await_args_list)
            + list(mock_platform.send_to_channel.await_args_list)
            + list(mock_platform.reply.await_args_list)
        ):
            text_args = [a for a in awaited.args if isinstance(a, str)]
            text_args += [v for k, v in awaited.kwargs.items()
                          if k in ("text", "body") and isinstance(v, str)]
            for t in text_args:
                assert "[CREATE_CONTINUOUS" not in t
                assert "CONTINUOUS_PROGRAM" not in t.upper()

    @pytest.mark.asyncio
    @patch("handlers.handle_delegations", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.handle_focus_commands", new_callable=AsyncMock,
           side_effect=lambda r, *a, **kw: r)
    @patch("handlers.split_message", side_effect=lambda text, max_len=4000: [text])
    async def test_malformed_macro_surfaces_prose_error_not_tokens(
        self, mock_split, mock_focus, mock_deleg,
        handlers, mock_platform, msg_ref, agent_manager,
    ):
        """FR-004: a malformed macro must produce a prose error, not a
        leaked JSON payload or raw tag."""
        malformed = (
            "Here you go.\n\n"
            '[CREATE_CONTINUOUS name="bad" work_dir="/etc/passwd"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective":"x","success_criteria":["y"],'
            '"first_step":{"number":1,"description":"z"}}\n'
            '[/CONTINUOUS_PROGRAM]'
        )

        with patch("handlers.invoke_ai", new_callable=AsyncMock, return_value=malformed):
            msg = make_message(text="please", thread_id=None)
            await handlers["message"](mock_platform, msg, msg_ref)

        seen_any_text = False
        for awaited in (
            list(mock_platform.send_message.await_args_list)
            + list(mock_platform.send_to_channel.await_args_list)
            + list(mock_platform.reply.await_args_list)
        ):
            text_args = [a for a in awaited.args if isinstance(a, str)]
            text_args += [v for k, v in awaited.kwargs.items()
                          if k in ("text", "body") and isinstance(v, str)]
            for t in text_args:
                seen_any_text = True
                assert "[CREATE_CONTINUOUS" not in t
                assert "CONTINUOUS_PROGRAM" not in t.upper()
                assert "/etc/passwd" not in t
        assert seen_any_text, "handler sent no message at all"
