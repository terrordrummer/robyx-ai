"""Tests for bot/ai_invoke.py — patterns, helpers, stream reading, and handlers."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_invoke import (
    AGENT_INSTRUCTIONS_PATTERN,
    CLOSE_WORKSPACE_PATTERN,
    CREATE_SPECIALIST_PATTERN,
    CREATE_WORKSPACE_PATTERN,
    DELEGATION_PATTERN,
    FOCUS_OFF_PATTERN,
    FOCUS_PATTERN,
    REMIND_PATTERN,
    REQUEST_PATTERN,
    RESTART_PATTERN,
    SPECIALIST_INSTRUCTIONS_PATTERN,
    STATUS_PATTERN,
    _classify_error,
    _is_rate_limited,
    _read_stream,
    handle_delegations,
    handle_focus_commands,
    handle_specialist_requests,
    invoke_ai,
    parse_remind_attrs,
    parse_remind_when,
    split_message,
)
from i18n import STRINGS


# ══════════════════════════════════════════════════════════════════════
# Regex Patterns
# ══════════════════════════════════════════════════════════════════════


class TestDelegationPattern:
    def test_basic_match(self):
        text = "[DELEGATE @builder: build the frontend]"
        m = DELEGATION_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "builder"
        assert m.group(2) == "build the frontend"

    def test_multiline_task(self):
        text = "[DELEGATE @ops: deploy the app\nand restart services]"
        m = DELEGATION_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "ops"
        assert "deploy the app" in m.group(2)
        assert "restart services" in m.group(2)


class TestFocusPattern:
    def test_match(self):
        m = FOCUS_PATTERN.search("[FOCUS @myagent]")
        assert m is not None
        assert m.group(1) == "myagent"

    def test_no_match_without_at(self):
        assert FOCUS_PATTERN.search("[FOCUS myagent]") is None


class TestFocusOffPattern:
    def test_lowercase(self):
        assert FOCUS_OFF_PATTERN.search("[FOCUS off]") is not None

    def test_uppercase(self):
        assert FOCUS_OFF_PATTERN.search("[FOCUS OFF]") is not None

    def test_mixed_case(self):
        assert FOCUS_OFF_PATTERN.search("[FOCUS Off]") is not None


class TestRestartPattern:
    def test_match(self):
        assert RESTART_PATTERN.search("[RESTART]") is not None

    def test_in_text(self):
        text = "Config updated.\n[RESTART]\nDone."
        assert RESTART_PATTERN.search(text) is not None

    def test_sub(self):
        text = "Config updated.\n[RESTART]\nDone."
        result = RESTART_PATTERN.sub("", text).strip()
        assert "[RESTART]" not in result
        assert "Config updated." in result

    def test_no_match_partial(self):
        assert RESTART_PATTERN.search("[RESTAR]") is None


class TestCreateWorkspacePattern:
    def test_full_match(self):
        text = '[CREATE_WORKSPACE name="analytics" type="scheduled" frequency="daily" model="sonnet" scheduled_at="09:00"]'
        m = CREATE_WORKSPACE_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "analytics"
        assert m.group(2) == "scheduled"
        assert m.group(3) == "daily"
        assert m.group(4) == "sonnet"
        assert m.group(5) == "09:00"


class TestAgentInstructionsPattern:
    def test_match(self):
        text = "[AGENT_INSTRUCTIONS]You are a coding assistant.[/AGENT_INSTRUCTIONS]"
        m = AGENT_INSTRUCTIONS_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "You are a coding assistant."

    def test_multiline(self):
        text = "[AGENT_INSTRUCTIONS]\nLine 1\nLine 2\n[/AGENT_INSTRUCTIONS]"
        m = AGENT_INSTRUCTIONS_PATTERN.search(text)
        assert m is not None
        assert "Line 1" in m.group(1)
        assert "Line 2" in m.group(1)


class TestCloseWorkspacePattern:
    def test_match(self):
        m = CLOSE_WORKSPACE_PATTERN.search('[CLOSE_WORKSPACE name="analytics"]')
        assert m is not None
        assert m.group(1) == "analytics"


class TestCreateSpecialistPattern:
    def test_match(self):
        m = CREATE_SPECIALIST_PATTERN.search('[CREATE_SPECIALIST name="reviewer" model="opus"]')
        assert m is not None
        assert m.group(1) == "reviewer"
        assert m.group(2) == "opus"


class TestSpecialistInstructionsPattern:
    def test_match(self):
        text = "[SPECIALIST_INSTRUCTIONS]Review all PRs carefully.[/SPECIALIST_INSTRUCTIONS]"
        m = SPECIALIST_INSTRUCTIONS_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "Review all PRs carefully."


class TestRequestPattern:
    def test_match(self):
        m = REQUEST_PATTERN.search("[REQUEST @reviewer: check this code]")
        assert m is not None
        assert m.group(1) == "reviewer"
        assert m.group(2) == "check this code"


class TestStatusPattern:
    def test_match(self):
        m = STATUS_PATTERN.search("[STATUS doing something]")
        assert m is not None
        assert m.group(1) == "doing something"


# ══════════════════════════════════════════════════════════════════════
# _is_rate_limited
# ══════════════════════════════════════════════════════════════════════


class TestIsRateLimited:
    @pytest.mark.parametrize("keyword", [
        "rate limit", "limit reached", "hit your limit", "too many requests",
        "usage cap", "over capacity", "quota exceeded", "throttl",
    ])
    def test_true_for_each_keyword(self, keyword):
        assert _is_rate_limited(f"Error: {keyword} exceeded") is True

    def test_false_for_normal_text(self):
        assert _is_rate_limited("Hello, how are you?") is False


# ══════════════════════════════════════════════════════════════════════
# _classify_error
# ══════════════════════════════════════════════════════════════════════


class TestClassifyError:
    def test_rate_limited(self):
        result = _classify_error("rate limit hit", "", "")
        assert result == STRINGS["rate_limited"]

    def test_network_error(self):
        result = _classify_error("connection refused", "", "")
        assert result == STRINGS["network_error"]

    def test_timeout_error(self):
        result = _classify_error("timeout waiting for response", "", "")
        assert result == STRINGS["network_error"]

    def test_permission_denied(self):
        result = _classify_error("permission denied for user", "", "")
        assert result == STRINGS["permission_denied"]

    def test_session_expired(self):
        result = _classify_error("session not found", "", "")
        assert result == STRINGS["session_expired"]

    def test_session_invalid(self):
        result = _classify_error("session invalid token", "", "")
        assert result == STRINGS["session_expired"]

    def test_unknown_error_with_stderr(self):
        result = _classify_error("something weird", "weird error", "")
        assert "weird error" in result

    def test_unknown_error_with_stdout(self):
        result = _classify_error("something weird", "", "stdout info")
        assert "stdout info" in result

    def test_unknown_error_fallback(self):
        result = _classify_error("something weird", "", "")
        assert "unknown" in result


# ══════════════════════════════════════════════════════════════════════
# split_message
# ══════════════════════════════════════════════════════════════════════


class TestSplitMessage:
    def test_short_text_single_element(self):
        assert split_message("hello", max_len=4000) == ["hello"]

    def test_empty_text(self):
        assert split_message("") == [""]

    def test_splits_on_newlines(self):
        text = ("A" * 100 + "\n") * 50  # 5050 chars with newlines
        chunks = split_message(text, max_len=4000)
        assert len(chunks) >= 2
        # Each chunk should be <= max_len
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_splits_on_spaces(self):
        # No newlines, only spaces
        text = ("word " * 1000).strip()  # ~4999 chars
        chunks = split_message(text, max_len=4000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_hard_break_no_good_split_point(self):
        # Single continuous string with no spaces or newlines
        text = "A" * 8000
        chunks = split_message(text, max_len=4000)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 4000
        assert chunks[1] == "A" * 4000


# ══════════════════════════════════════════════════════════════════════
# _read_stream (async)
# ══════════════════════════════════════════════════════════════════════


def _make_mock_proc(lines):
    """Create a mock async subprocess with stdout that yields given byte lines."""
    mock_proc = AsyncMock()
    # readline returns each line in sequence, then b'' for EOF
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.readline = AsyncMock(side_effect=lines)
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stderr = AsyncMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    return mock_proc


class TestReadStream:
    @pytest.mark.asyncio
    async def test_extracts_result(self, mock_bot):
        mock_proc = _make_mock_proc([
            b'{"type":"system","subtype":"init"}\n',
            b'{"type":"result","subtype":"success","result":"final answer"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result == "final answer"

    @pytest.mark.asyncio
    async def test_detects_status_and_sends_to_bot(self, mock_bot):
        mock_proc = _make_mock_proc([
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"[STATUS Analyzing code] working"}]}}\n',
            b'{"type":"result","subtype":"success","result":"done"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result == "done"
        # Bot should have been called with the status message
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert "Analyzing code" in call_kwargs.kwargs.get("text", call_kwargs[1].get("text", ""))

    @pytest.mark.asyncio
    async def test_strips_status_from_result(self, mock_bot):
        mock_proc = _make_mock_proc([
            b'{"type":"result","subtype":"success","result":"[STATUS Doing X] hello world"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert "[STATUS" not in result
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_returns_none_if_no_result_event(self, mock_bot):
        mock_proc = _make_mock_proc([
            b'{"type":"system","subtype":"init"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_timeout(self, mock_bot):
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        backend = MagicMock()

        with pytest.raises(asyncio.TimeoutError):
            await _read_stream(mock_proc, mock_bot, 123, 1, backend)

    @pytest.mark.asyncio
    async def test_skips_malformed_json(self, mock_bot):
        mock_proc = _make_mock_proc([
            b"this is not json\n",
            b'{"type":"result","subtype":"success","result":"ok"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result == "ok"


# ══════════════════════════════════════════════════════════════════════
# handle_delegations (async)
# ══════════════════════════════════════════════════════════════════════


class TestHandleDelegations:
    @pytest.mark.asyncio
    async def test_no_delegation_returns_unchanged(self, mock_bot, agent_manager, claude_backend):
        result = await handle_delegations(
            "just a normal response", 123, mock_bot, agent_manager, claude_backend,
        )
        assert result == "just a normal response"

    @pytest.mark.asyncio
    async def test_delegation_to_existing_agent(self, mock_bot, agent_manager, claude_backend, tmp_path):
        agent_manager.add_agent("builder", str(tmp_path), "Builder agent")

        with patch("ai_invoke.invoke_ai", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = "Built successfully"
            result = await handle_delegations(
                "OK [DELEGATE @builder: build the app]",
                123, mock_bot, agent_manager, claude_backend,
            )
        assert "Built successfully" in result
        assert "builder" in result.lower()

    @pytest.mark.asyncio
    async def test_delegation_to_missing_agent(self, mock_bot, agent_manager, claude_backend):
        result = await handle_delegations(
            "[DELEGATE @nonexistent: do something]",
            123, mock_bot, agent_manager, claude_backend,
        )
        assert "nonexistent" in result.lower()
        assert "not active" in result.lower() or "missing" in result.lower() or "activate" in result.lower()

    @pytest.mark.asyncio
    async def test_multiple_delegations(self, mock_bot, agent_manager, claude_backend, tmp_path):
        agent_manager.add_agent("alpha", str(tmp_path), "Alpha")
        agent_manager.add_agent("beta", str(tmp_path), "Beta")

        with patch("ai_invoke.invoke_ai", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.side_effect = ["Alpha result", "Beta result"]
            result = await handle_delegations(
                "Delegating: [DELEGATE @alpha: task A] [DELEGATE @beta: task B]",
                123, mock_bot, agent_manager, claude_backend,
            )
        assert "Alpha result" in result
        assert "Beta result" in result


# ══════════════════════════════════════════════════════════════════════
# handle_focus_commands (async)
# ══════════════════════════════════════════════════════════════════════


class TestHandleFocusCommands:
    @pytest.mark.asyncio
    async def test_focus_off(self, mock_bot, agent_manager):
        agent_manager.focused_agent = "someagent"
        result = await handle_focus_commands(
            "Sure. [FOCUS off] Done.", 123, mock_bot, agent_manager,
        )
        assert agent_manager.focused_agent is None
        assert "[FOCUS off]" not in result
        # Should have sent focus_off message
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_focus_on_existing_agent(self, mock_bot, agent_manager, tmp_path):
        agent_manager.add_agent("builder", str(tmp_path), "Builder")
        result = await handle_focus_commands(
            "Focusing. [FOCUS @builder]", 123, mock_bot, agent_manager,
        )
        assert agent_manager.focused_agent == "builder"
        assert "[FOCUS @builder]" not in result
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_focus_on_unknown_agent(self, mock_bot, agent_manager):
        agent_manager.focused_agent = None  # ensure clean state
        result = await handle_focus_commands(
            "[FOCUS @ghost]", 123, mock_bot, agent_manager,
        )
        assert "[FOCUS @ghost]" not in result
        # focused_agent should remain unchanged (None) since ghost doesn't exist
        assert agent_manager.focused_agent is None
        mock_bot.send_message.assert_called_once()
        call_text = mock_bot.send_message.call_args.kwargs.get(
            "text", mock_bot.send_message.call_args[1].get("text", "")
        )
        assert "ghost" in call_text.lower()

    @pytest.mark.asyncio
    async def test_no_focus_pattern(self, mock_bot, agent_manager):
        result = await handle_focus_commands(
            "No focus here", 123, mock_bot, agent_manager,
        )
        assert result == "No focus here"
        mock_bot.send_message.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# handle_specialist_requests (async)
# ══════════════════════════════════════════════════════════════════════


class TestHandleSpecialistRequests:
    @pytest.mark.asyncio
    async def test_request_to_specialist(self, mock_bot, agent_manager, claude_backend, tmp_path):
        specialist = agent_manager.add_agent(
            "reviewer", str(tmp_path), "Code reviewer", agent_type="specialist",
        )
        requester = agent_manager.get("robyx")

        with patch("ai_invoke.invoke_ai", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = "LGTM"
            result = await handle_specialist_requests(
                "Need review. [REQUEST @reviewer: check code]",
                123, mock_bot, agent_manager, claude_backend, requester,
            )
        assert "LGTM" in result
        assert "reviewer" in result.lower()

    @pytest.mark.asyncio
    async def test_missing_specialist(self, mock_bot, agent_manager, claude_backend):
        requester = agent_manager.get("robyx")
        result = await handle_specialist_requests(
            "[REQUEST @nobody: do thing]",
            123, mock_bot, agent_manager, claude_backend, requester,
        )
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_non_specialist_agent(self, mock_bot, agent_manager, claude_backend, tmp_path):
        # Add a workspace agent (not a specialist)
        agent_manager.add_agent("worker", str(tmp_path), "Worker", agent_type="workspace")
        requester = agent_manager.get("robyx")
        result = await handle_specialist_requests(
            "[REQUEST @worker: do thing]",
            123, mock_bot, agent_manager, claude_backend, requester,
        )
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_no_request_pattern(self, mock_bot, agent_manager, claude_backend):
        requester = agent_manager.get("robyx")
        result = await handle_specialist_requests(
            "Nothing special here",
            123, mock_bot, agent_manager, claude_backend, requester,
        )
        assert result == "Nothing special here"


# ══════════════════════════════════════════════════════════════════════
# invoke_ai (async)
# ══════════════════════════════════════════════════════════════════════


class TestInvokeAi:
    @pytest.mark.asyncio
    async def test_acquires_agent_lock(self, mock_bot, agent_manager, claude_backend):
        agent = agent_manager.get("robyx")
        lock_acquired = False

        async def fake_locked(*args, **kwargs):
            nonlocal lock_acquired
            # If we got here, the lock wrapper called us while holding the lock
            lock_acquired = True
            return "response"

        with patch("ai_invoke._invoke_ai_locked", new=fake_locked):
            result = await invoke_ai(
                agent, "hello", 123, mock_bot, agent_manager, claude_backend,
            )
        assert lock_acquired is True
        assert result == "response"

    @pytest.mark.asyncio
    async def test_basic_flow(self, mock_bot, agent_manager, claude_backend):
        agent = agent_manager.get("robyx")

        with patch("ai_invoke._invoke_ai_locked", new_callable=AsyncMock) as mock_locked:
            mock_locked.return_value = "AI says hello"
            result = await invoke_ai(
                agent, "hello", 123, mock_bot, agent_manager, claude_backend,
            )
        assert result == "AI says hello"
        mock_locked.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════
# _invoke_ai_locked (async) — lines 102-238
# ══════════════════════════════════════════════════════════════════════


from ai_invoke import _invoke_ai_locked
from config import (
    AI_TIMEOUT,
    ROBYX_SYSTEM_PROMPT,
    FOCUSED_AGENT_SYSTEM_PROMPT,
    WORKSPACE_AGENT_SYSTEM_PROMPT,
)


def _make_mock_process(stdout_data=b"", stderr_data=b"", returncode=0):
    """Create a mock subprocess for _invoke_ai_locked tests."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.stdout = AsyncMock()
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr_data)
    proc.wait = AsyncMock()
    return proc


@pytest.fixture(autouse=True)
def _patch_memory_in_invoke():
    """Patch memory functions to return empty strings in ai_invoke tests."""
    with patch("ai_invoke.build_memory_context", return_value=""), \
         patch("ai_invoke.get_memory_instructions", return_value=""):
        yield


class TestInvokeAiLocked:
    """Tests for _invoke_ai_locked covering lines 107-238."""

    @pytest.mark.asyncio
    async def test_system_prompt_robyx(self, agent_manager, mock_bot, claude_backend):
        """Line 108-109: agent.name == 'robyx' -> ROBYX_SYSTEM_PROMPT."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude", "-p", "hi"]) as mock_build, \
             patch.object(claude_backend, "parse_response", return_value="response"):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(agent, "hello", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        # Verify system_prompt contains ROBYX_SYSTEM_PROMPT
        call_kwargs = mock_build.call_args
        prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
        assert ROBYX_SYSTEM_PROMPT in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_focused(self, agent_manager, mock_bot, claude_backend, tmp_path):
        """Line 110-111: focused_agent == agent.name -> FOCUSED_AGENT_SYSTEM_PROMPT."""
        agent_manager.add_agent("builder", str(tmp_path), "Builder")
        agent_manager.set_focus("builder")
        agent = agent_manager.get("builder")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]) as mock_build, \
             patch.object(claude_backend, "parse_response", return_value="ok"):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        call_kwargs = mock_build.call_args
        prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
        assert FOCUSED_AGENT_SYSTEM_PROMPT in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_workspace(self, agent_manager, mock_bot, claude_backend, tmp_path):
        """Line 112-113: workspace agent -> WORKSPACE_AGENT_SYSTEM_PROMPT."""
        agent_manager.add_agent("worker", str(tmp_path), "Worker", agent_type="workspace")
        agent = agent_manager.get("worker")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]) as mock_build, \
             patch.object(claude_backend, "parse_response", return_value="ok"):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        call_kwargs = mock_build.call_args
        prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
        assert WORKSPACE_AGENT_SYSTEM_PROMPT in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_specialist(self, agent_manager, mock_bot, claude_backend, tmp_path):
        """Line 112-113: specialist agent -> WORKSPACE_AGENT_SYSTEM_PROMPT."""
        agent_manager.add_agent("reviewer", str(tmp_path), "Reviewer", agent_type="specialist")
        agent = agent_manager.get("reviewer")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]) as mock_build, \
             patch.object(claude_backend, "parse_response", return_value="ok"):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        call_kwargs = mock_build.call_args
        prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
        assert WORKSPACE_AGENT_SYSTEM_PROMPT in prompt

    @pytest.mark.asyncio
    async def test_workspace_invocation_uses_stored_work_dir_for_memory_and_subprocess(
        self, agent_manager, mock_bot, claude_backend, tmp_path
    ):
        agent_manager.add_agent(
            "builder",
            str(tmp_path / "real-worktree"),
            "Builder",
            agent_type="workspace",
        )
        agent = agent_manager.get("builder")
        proc = _make_mock_process()

        with patch(
            "ai_invoke.build_memory_context",
            return_value="MEMORY",
        ) as mock_memory, patch(
            "ai_invoke.get_memory_instructions",
            return_value="INSTRUCTIONS",
        ) as mock_memory_instr, patch(
            "ai_invoke.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec, patch.object(
            claude_backend, "supports_streaming", return_value=False,
        ), patch.object(
            claude_backend, "build_command", return_value=["claude"],
        ) as mock_build, patch.object(
            claude_backend, "parse_response", return_value="ok",
        ):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(
                agent, "hi", 123, mock_bot, agent_manager, claude_backend,
                False, "sonnet", 0, None,
            )

        work_dir = str(tmp_path / "real-worktree")
        mock_memory.assert_called_once_with("builder", "workspace", work_dir)
        mock_memory_instr.assert_called_once_with("builder", "workspace", work_dir)
        assert mock_build.call_args.kwargs["work_dir"] == work_dir
        assert mock_exec.call_args.kwargs["cwd"] == work_dir

    @pytest.mark.asyncio
    async def test_system_prompt_none_for_unknown_type(self, agent_manager, mock_bot, claude_backend, tmp_path):
        """Line 107: system_prompt stays None if no match."""
        agent_manager.add_agent("custom", str(tmp_path), "Custom", agent_type="other")
        agent = agent_manager.get("custom")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]) as mock_build, \
             patch.object(claude_backend, "parse_response", return_value="ok"):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"output", b""))
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        call_kwargs = mock_build.call_args
        assert call_kwargs.kwargs.get("system_prompt") is None or \
               call_kwargs[1].get("system_prompt") is None

    @pytest.mark.asyncio
    async def test_non_streaming_success(self, agent_manager, mock_bot, claude_backend):
        """Lines 164-190: non-streaming path with successful response."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(stdout_data=b'{"type":"result","result":"hello world"}', returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch.object(claude_backend, "parse_response", return_value="hello world") as mock_parse:
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == "hello world"
        mock_parse.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_streaming_error_returncode(self, agent_manager, mock_bot, claude_backend):
        """Lines 170-185: proc.returncode != 0 -> classify_error."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(stdout_data=b"", stderr_data=b"connection refused", returncode=1)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["network_error"]

    @pytest.mark.asyncio
    async def test_non_streaming_retry_session_collision(self, agent_manager, mock_bot, claude_backend):
        """Lines 175-184: 'already in use' in output -> recursive retry."""
        agent = agent_manager.get("robyx")
        proc_fail = _make_mock_process(stdout_data=b"session already in use", stderr_data=b"", returncode=1)
        proc_ok = _make_mock_process(stdout_data=b"ok", returncode=0)

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc_fail
            return proc_ok

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch.object(claude_backend, "parse_response", return_value="recovered"):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_session_collision_regenerates_session_id(self, agent_manager, mock_bot, claude_backend):
        """On session collision the retry MUST regenerate agent.session_id.
        Reusing the same id is futile and historically caused keepalive to
        type forever because the retry would hang on --resume of a broken
        session."""
        agent = agent_manager.get("robyx")
        original_sid = agent.session_id
        proc_fail = _make_mock_process(
            stdout_data=b"session already in use", stderr_data=b"", returncode=1,
        )
        proc_ok = _make_mock_process(stdout_data=b"ok", returncode=0)

        call_count = 0

        async def fake_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc_fail
            return proc_ok

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_run), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch.object(claude_backend, "parse_response", return_value="recovered"):
            await _invoke_ai_locked(
                agent, "hi", 123, mock_bot, agent_manager, claude_backend,
                False, "sonnet", 0, None,
            )

        assert agent.session_id != original_sid
        import uuid as _u
        _u.UUID(agent.session_id)  # raises if the regenerated id is not a valid UUID

    @pytest.mark.asyncio
    async def test_stream_idle_in_result_triggers_retry(self, agent_manager, mock_bot, claude_backend):
        """Transient stream error delivered as result payload -> retry with fresh session."""
        agent = agent_manager.get("robyx")
        original_sid = agent.session_id
        call_count = 0

        async def fake_spawn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_mock_process(returncode=0)

        responses = iter([
            "API Error: Stream idle timeout - partial response received",
            "recovered",
        ])

        async def fake_read_stream(*args, **kwargs):
            return next(responses)

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", side_effect=fake_read_stream):
            result = await _invoke_ai_locked(
                agent, "hi", 123, mock_bot, agent_manager, claude_backend,
                False, "sonnet", 0, None,
            )

        assert result == "recovered"
        assert call_count == 2
        assert agent.session_id != original_sid

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend_fixture", ["claude_backend", "codex_backend", "opencode_backend"])
    async def test_stream_retryable_works_for_all_backends(
        self, request, agent_manager, mock_bot, backend_fixture,
    ):
        """Stream-retryable detection + retry must fire uniformly for every
        backend, not just Claude Code. We hit the non-streaming path so the
        test is backend-agnostic (streaming is Claude-only)."""
        backend = request.getfixturevalue(backend_fixture)
        agent = agent_manager.get("robyx")
        proc_fail = _make_mock_process(
            stdout_data=b"",
            stderr_data=b"socket hang up",
            returncode=1,
        )
        proc_ok = _make_mock_process(stdout_data=b"ok", returncode=0)
        call_count = 0

        async def fake_spawn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return proc_fail if call_count == 1 else proc_ok

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(backend, "supports_streaming", return_value=False), \
             patch.object(backend, "build_command", return_value=["cli"]), \
             patch.object(backend, "parse_response", return_value="recovered"):
            result = await _invoke_ai_locked(
                agent, "hi", 123, mock_bot, agent_manager, backend,
                False, "sonnet", 0, None,
            )

        assert result == "recovered", "retry should produce the recovered response"
        assert call_count == 2, "backend %s should have retried once" % backend_fixture

    @pytest.mark.asyncio
    async def test_stream_idle_in_stderr_triggers_retry(self, agent_manager, mock_bot, claude_backend):
        """Transient stream error on stderr (non-streaming) -> retry, not raw error."""
        agent = agent_manager.get("robyx")
        original_sid = agent.session_id
        proc_fail = _make_mock_process(
            stdout_data=b"",
            stderr_data=b"API Error: Stream idle timeout - partial response received",
            returncode=1,
        )
        proc_ok = _make_mock_process(stdout_data=b"ok", returncode=0)
        call_count = 0

        async def fake_spawn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return proc_fail if call_count == 1 else proc_ok

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_spawn), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch.object(claude_backend, "parse_response", return_value="recovered"):
            result = await _invoke_ai_locked(
                agent, "hi", 123, mock_bot, agent_manager, claude_backend,
                False, "sonnet", 0, None,
            )

        assert result == "recovered"
        assert call_count == 2
        assert agent.session_id != original_sid

    @pytest.mark.asyncio
    async def test_non_streaming_empty_stdout(self, agent_manager, mock_bot, claude_backend):
        """Line 187-188: empty stdout -> ai_no_response."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(stdout_data=b"", returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["ai_no_response"]

    @pytest.mark.asyncio
    async def test_streaming_success(self, agent_manager, mock_bot, claude_backend):
        """Lines 162-163: streaming path returns valid text."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value="streamed response"):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == "streamed response"

    @pytest.mark.asyncio
    async def test_streaming_returns_none_error(self, agent_manager, mock_bot, claude_backend):
        """Lines 193-212: _read_stream returns None, proc.returncode != 0."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(stderr_data=b"permission denied", returncode=1)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value=None):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["permission_denied"]

    @pytest.mark.asyncio
    async def test_streaming_returns_none_retry(self, agent_manager, mock_bot, claude_backend):
        """Lines 202-211: _read_stream returns None, 'already in use' -> retry."""
        agent = agent_manager.get("robyx")
        proc_fail = _make_mock_process(stderr_data=b"session already in use", returncode=1)
        proc_ok = _make_mock_process(returncode=0)

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc_fail
            return proc_ok

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, side_effect=[None, "ok after retry"]):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == "ok after retry"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_streaming_returns_none_ok(self, agent_manager, mock_bot, claude_backend):
        """Line 213: _read_stream returns None, returncode == 0 -> ai_no_response."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value=None):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["ai_no_response"]

    @pytest.mark.asyncio
    async def test_rate_limited_response(self, agent_manager, mock_bot, claude_backend):
        """Lines 215-217: response contains rate limit keyword."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value="rate limit reached sorry"):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["rate_limited"]

    @pytest.mark.asyncio
    async def test_empty_response(self, agent_manager, mock_bot, claude_backend):
        """Line 218: parse_response returns empty string -> ai_empty."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process(stdout_data=b"something", returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch.object(claude_backend, "parse_response", return_value=""):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert result == STRINGS["ai_empty"]

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, agent_manager, mock_bot, claude_backend):
        """Lines 225-231: asyncio.TimeoutError -> proc.kill() called."""
        agent = agent_manager.get("robyx")
        proc = _make_mock_process()

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "supports_streaming", return_value=False), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert str(AI_TIMEOUT) in result
        proc.kill.assert_called_once()
        assert agent.busy is False  # finally block

    @pytest.mark.asyncio
    async def test_general_exception(self, agent_manager, mock_bot, claude_backend):
        """Lines 232-234: random exception -> ai_error string."""
        agent = agent_manager.get("robyx")

        with patch("ai_invoke.asyncio.create_subprocess_exec", side_effect=RuntimeError("boom")), \
             patch.object(claude_backend, "build_command", return_value=["claude"]):
            result = await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert "boom" in result
        assert agent.busy is False  # finally block

    @pytest.mark.asyncio
    async def test_agent_state_updated_on_success(self, agent_manager, mock_bot, claude_backend):
        """Lines 220-223: last_used, message_count, session_started updated."""
        agent = agent_manager.get("robyx")
        old_count = agent.message_count
        agent.session_started = False
        proc = _make_mock_process(returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value="success"):
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert agent.message_count == old_count + 1
        assert agent.session_started is True
        assert agent.busy is False

    @pytest.mark.asyncio
    async def test_finally_cleans_up(self, agent_manager, mock_bot, claude_backend):
        """Finally block clears busy flag and running_proc reference."""
        agent = agent_manager.get("robyx")
        agent.busy = True
        proc = _make_mock_process(returncode=0)

        with patch("ai_invoke.asyncio.create_subprocess_exec", return_value=proc), \
             patch.object(claude_backend, "build_command", return_value=["claude"]), \
             patch("ai_invoke._read_stream", new_callable=AsyncMock, return_value="done"):
            await _invoke_ai_locked(agent, "hi", 123, mock_bot, agent_manager, claude_backend, False, "sonnet", 0, None)

        assert agent.busy is False


# ══════════════════════════════════════════════════════════════════════
# _read_stream edge cases — lines 252, 261-262, 269-270, 283
# ══════════════════════════════════════════════════════════════════════


class TestReadStreamEdgeCases:
    @pytest.mark.asyncio
    async def test_duplicate_status_not_sent_twice(self, mock_bot):
        """Line 251-252: duplicate status message is skipped."""
        mock_proc = _make_mock_proc([
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"[STATUS Analyzing] working"}]}}\n',
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"[STATUS Analyzing] still going"}]}}\n',
            b'{"type":"result","subtype":"success","result":"done"}\n',
            b"",
        ])
        backend = MagicMock()
        await _read_stream(mock_proc, mock_bot, 123, 1, backend)

        # "Analyzing" status should only be sent once (duplicate skipped)
        assert mock_bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_send_status_exception_handled(self, mock_bot):
        """Lines 261-262: exception in _send_status is caught and logged."""
        mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API error"))

        mock_proc = _make_mock_proc([
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"[STATUS Working] stuff"}]}}\n',
            b'{"type":"result","subtype":"success","result":"final"}\n',
            b"",
        ])
        backend = MagicMock()
        # Should not raise despite send_message failing
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result == "final"

    @pytest.mark.asyncio
    async def test_timeout_in_read_stream(self, mock_bot):
        """Lines 268-270: remaining <= 0 kills proc and raises TimeoutError."""
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        backend = MagicMock()

        # Patch the event loop time so remaining is immediately <= 0
        with patch("ai_invoke.AI_TIMEOUT", 0):
            with pytest.raises(asyncio.TimeoutError):
                await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        mock_proc.kill.assert_called()

    @pytest.mark.asyncio
    async def test_empty_line_skipped(self, mock_bot):
        """Line 282-283: empty line after decode is skipped (continue)."""
        mock_proc = _make_mock_proc([
            b"\n",
            b"   \n",
            b'{"type":"result","subtype":"success","result":"ok"}\n',
            b"",
        ])
        backend = MagicMock()
        result = await _read_stream(mock_proc, mock_bot, 123, 1, backend)
        assert result == "ok"


# ══════════════════════════════════════════════════════════════════════
# handle_delegations edge cases — line 345-346
# ══════════════════════════════════════════════════════════════════════


class TestHandleDelegationsEdgeCases:
    @pytest.mark.asyncio
    async def test_send_message_exception_handled(self, mock_bot, agent_manager, claude_backend, tmp_path):
        """Lines 345-346: bot.send_message raises but delegation continues."""
        agent_manager.add_agent("builder", str(tmp_path), "Builder")
        mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))

        with patch("ai_invoke.invoke_ai", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = "Built ok"
            result = await handle_delegations(
                "[DELEGATE @builder: build app]",
                123, mock_bot, agent_manager, claude_backend,
            )

        # Delegation should still succeed despite send_message failure
        assert "Built ok" in result


# ══════════════════════════════════════════════════════════════════════
# handle_focus_commands edge cases — lines 374-375, 394-395, 404-405
# ══════════════════════════════════════════════════════════════════════


class TestHandleFocusEdgeCases:
    @pytest.mark.asyncio
    async def test_focus_on_send_fails(self, mock_bot, agent_manager, tmp_path):
        """Lines 374-375 (mapped to 387-395): send_message exception for focus_on."""
        agent_manager.add_agent("builder", str(tmp_path), "Builder")
        mock_bot.send_message = AsyncMock(side_effect=Exception("send failed"))

        # Should not raise despite send_message failing
        result = await handle_focus_commands(
            "[FOCUS @builder]", 123, mock_bot, agent_manager,
        )
        assert agent_manager.focused_agent == "builder"
        assert "[FOCUS @builder]" not in result

    @pytest.mark.asyncio
    async def test_focus_unknown_send_fails(self, mock_bot, agent_manager):
        """Lines 394-395 (mapped to 397-405): send_message exception for agent_not_found."""
        mock_bot.send_message = AsyncMock(side_effect=Exception("send failed"))

        result = await handle_focus_commands(
            "[FOCUS @ghost]", 123, mock_bot, agent_manager,
        )
        assert agent_manager.focused_agent is None
        assert "[FOCUS @ghost]" not in result

    @pytest.mark.asyncio
    async def test_focus_off_send_fails(self, mock_bot, agent_manager):
        """Lines 404-405 (mapped to 367-375): send_message exception for focus_off."""
        agent_manager.focused_agent = "something"
        mock_bot.send_message = AsyncMock(side_effect=Exception("send failed"))

        result = await handle_focus_commands(
            "[FOCUS off]", 123, mock_bot, agent_manager,
        )
        assert agent_manager.focused_agent is None
        assert "[FOCUS off]" not in result


# ══════════════════════════════════════════════════════════════════════
# handle_specialist_requests edge cases — lines 438-439
# ══════════════════════════════════════════════════════════════════════


class TestHandleSpecialistEdgeCases:
    @pytest.mark.asyncio
    async def test_send_message_exception(self, mock_bot, agent_manager, claude_backend, tmp_path):
        """Lines 438-439: bot.send_message raises but specialist request continues."""
        agent_manager.add_agent("reviewer", str(tmp_path), "Code reviewer", agent_type="specialist")
        requester = agent_manager.get("robyx")
        mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))

        with patch("ai_invoke.invoke_ai", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = "LGTM"
            result = await handle_specialist_requests(
                "[REQUEST @reviewer: check code]",
                123, mock_bot, agent_manager, claude_backend, requester,
            )

        # Request should still succeed despite send_message failure
        assert "LGTM" in result


# ═══════════════════════════════════════════════════════════════════════════
# _normalize_backend_response — backends may return str OR dict
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeBackendResponse:
    """Backends used to return only ``str``. OpenCode now returns
    ``{text, session_id}``. The normaliser hides the difference."""

    def test_string_payload_returns_no_session_id(self):
        from ai_invoke import _normalize_backend_response

        text, sid = _normalize_backend_response("hello")
        assert text == "hello"
        assert sid is None

    def test_dict_payload_extracts_text_and_session(self):
        from ai_invoke import _normalize_backend_response

        text, sid = _normalize_backend_response(
            {"text": "hi", "session_id": "ses_42"},
        )
        assert text == "hi"
        assert sid == "ses_42"

    def test_dict_payload_missing_keys_defaults(self):
        from ai_invoke import _normalize_backend_response

        text, sid = _normalize_backend_response({"text": ""})
        assert text == ""
        assert sid is None

    def test_none_payload_is_safe(self):
        from ai_invoke import _normalize_backend_response

        text, sid = _normalize_backend_response(None)
        assert text == ""
        assert sid is None


# ═══════════════════════════════════════════════════════════════════════════
# _agent_model_role — maps an Agent to a role key for models.yaml
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentModelRole:
    def test_robyx_is_orchestrator(self):
        from agents import Agent
        from ai_invoke import _agent_model_role

        robyx = Agent(name="robyx", work_dir="/", description="orch", agent_type="orchestrator")
        assert _agent_model_role(robyx) == "orchestrator"

    def test_specialist_role(self):
        from agents import Agent
        from ai_invoke import _agent_model_role

        a = Agent(name="rev", work_dir="/", description="r", agent_type="specialist")
        assert _agent_model_role(a) == "specialist"

    def test_workspace_is_default(self):
        from agents import Agent
        from ai_invoke import _agent_model_role

        a = Agent(name="ws", work_dir="/", description="w", agent_type="workspace")
        assert _agent_model_role(a) == "workspace"


# ═══════════════════════════════════════════════════════════════════════════
# _load_agent_instructions — interactive turns must see the agent's brief
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadAgentInstructions:
    """Workspace and specialist agents have a markdown brief at
    ``agents/<name>.md`` / ``specialists/<name>.md``. The interactive
    invocation path used to ignore them — only scheduled spawns saw the
    full brief, which made interactive runs reply with vague defaults."""

    def test_loads_workspace_markdown(self, tmp_path, monkeypatch):
        import ai_invoke
        from agents import Agent

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(exist_ok=True)
        (agents_dir / "alpha.md").write_text("# Alpha\nBe terse.\n")
        monkeypatch.setattr(ai_invoke, "AGENTS_DIR", agents_dir)

        a = Agent(name="alpha", work_dir="/", description="x", agent_type="workspace")
        loaded = ai_invoke._load_agent_instructions(a)
        assert "## Agent Instructions" in loaded
        assert "Be terse." in loaded

    def test_loads_specialist_markdown(self, tmp_path, monkeypatch):
        import ai_invoke
        from agents import Agent

        spec_dir = tmp_path / "specialists"
        spec_dir.mkdir(exist_ok=True)
        (spec_dir / "rev.md").write_text("Reviewer brief")
        monkeypatch.setattr(ai_invoke, "SPECIALISTS_DIR", spec_dir)

        a = Agent(name="rev", work_dir="/", description="x", agent_type="specialist")
        loaded = ai_invoke._load_agent_instructions(a)
        assert "Reviewer brief" in loaded

    def test_orchestrator_returns_empty(self, tmp_path):
        import ai_invoke
        from agents import Agent

        a = Agent(name="robyx", work_dir="/", description="x", agent_type="orchestrator")
        assert ai_invoke._load_agent_instructions(a) == ""

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import ai_invoke
        from agents import Agent

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(ai_invoke, "AGENTS_DIR", agents_dir)

        a = Agent(name="ghost", work_dir="/", description="x", agent_type="workspace")
        assert ai_invoke._load_agent_instructions(a) == ""

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        import ai_invoke
        from agents import Agent

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(exist_ok=True)
        (agents_dir / "empty.md").write_text("   \n  \n")
        monkeypatch.setattr(ai_invoke, "AGENTS_DIR", agents_dir)

        a = Agent(name="empty", work_dir="/", description="x", agent_type="workspace")
        assert ai_invoke._load_agent_instructions(a) == ""


# ══════════════════════════════════════════════════════════════════════
# REMIND pattern + parser
# ══════════════════════════════════════════════════════════════════════


class TestRemindPattern:
    def test_basic_in(self):
        m = REMIND_PATTERN.search('[REMIND in="2m" text="hello"]')
        assert m is not None
        attrs = parse_remind_attrs(m.group(1))
        assert attrs == {"in": "2m", "text": "hello"}

    def test_basic_at(self):
        m = REMIND_PATTERN.search(
            '[REMIND at="2026-04-09T09:00:00+02:00" text="dentist"]'
        )
        assert m is not None
        attrs = parse_remind_attrs(m.group(1))
        assert attrs["at"] == "2026-04-09T09:00:00+02:00"
        assert attrs["text"] == "dentist"

    def test_attribute_order_independent(self):
        m = REMIND_PATTERN.search('[REMIND text="hi" in="90s"]')
        assert m is not None
        attrs = parse_remind_attrs(m.group(1))
        assert attrs == {"text": "hi", "in": "90s"}

    def test_with_thread(self):
        m = REMIND_PATTERN.search(
            '[REMIND in="1h30m" text="standup" thread="903"]'
        )
        attrs = parse_remind_attrs(m.group(1))
        assert attrs == {"in": "1h30m", "text": "standup", "thread": "903"}

    def test_unicode_in_text(self):
        m = REMIND_PATTERN.search('[REMIND in="2m" text="⏰ caffè ☕"]')
        attrs = parse_remind_attrs(m.group(1))
        assert attrs["text"] == "⏰ caffè ☕"

    def test_multiple_in_one_response(self):
        text = (
            'Sure!\n'
            '[REMIND in="1h" text="A"]\n'
            '[REMIND in="2h" text="B"]\n'
            'Done.'
        )
        matches = list(REMIND_PATTERN.finditer(text))
        assert len(matches) == 2
        assert parse_remind_attrs(matches[0].group(1))["text"] == "A"
        assert parse_remind_attrs(matches[1].group(1))["text"] == "B"

    def test_sub_strips_pattern(self):
        text = (
            'Done.\n[REMIND in="2m" text="hi"]\nBye.'
        )
        result = REMIND_PATTERN.sub("", text).strip()
        assert "[REMIND" not in result
        assert "Done." in result
        assert "Bye." in result

    def test_no_match_partial(self):
        assert REMIND_PATTERN.search("[REMIND]") is None
        assert REMIND_PATTERN.search('[REMINDx in="2m" text="x"]') is None


class TestParseRemindWhen:
    def test_in_seconds(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        result = parse_remind_when(at=None, in_="90s", now=now)
        assert result == datetime(2026, 4, 8, 12, 1, 30, tzinfo=timezone.utc)

    def test_in_minutes(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_remind_when(at=None, in_="2m", now=now) == datetime(
            2026, 4, 8, 12, 2, 0, tzinfo=timezone.utc
        )

    def test_in_compound_duration(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        result = parse_remind_when(at=None, in_="1h30m", now=now)
        assert result == datetime(2026, 4, 8, 13, 30, 0, tzinfo=timezone.utc)

    def test_in_days(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_remind_when(at=None, in_="2d", now=now) == datetime(
            2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc
        )

    def test_at_with_offset_normalised_to_utc(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 6, 0, 0, tzinfo=timezone.utc)
        result = parse_remind_when(
            at="2026-04-09T09:00:00+02:00", in_=None, now=now
        )
        assert result == datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)

    def test_neither_at_nor_in_rejected(self):
        with pytest.raises(ValueError, match="exactly one"):
            parse_remind_when(at=None, in_=None)

    def test_both_at_and_in_rejected(self):
        with pytest.raises(ValueError, match="exactly one"):
            parse_remind_when(at="2026-04-09T09:00:00+02:00", in_="2m")

    def test_at_without_timezone_rejected(self):
        with pytest.raises(ValueError, match="timezone"):
            parse_remind_when(at="2026-04-09T09:00:00", in_=None)

    def test_at_in_past_rejected(self):
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="past"):
            parse_remind_when(at="2026-04-08T11:00:00+00:00", in_=None, now=now)

    def test_at_recent_past_within_60s_tolerated(self):
        """A 30s past `at=` is accepted (clock skew tolerance)."""
        from datetime import datetime, timezone

        now = datetime(2026, 4, 8, 12, 0, 30, tzinfo=timezone.utc)
        result = parse_remind_when(
            at="2026-04-08T12:00:00+00:00", in_=None, now=now
        )
        assert result == datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)

    def test_at_malformed_rejected(self):
        with pytest.raises(ValueError, match="invalid at"):
            parse_remind_when(at="not-a-date", in_=None)

    def test_in_invalid_format_rejected(self):
        with pytest.raises(ValueError, match="invalid in"):
            parse_remind_when(at=None, in_="forever")

    def test_in_zero_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            parse_remind_when(at=None, in_="0s")

    def test_in_over_90_days_rejected(self):
        with pytest.raises(ValueError, match="90 days"):
            parse_remind_when(at=None, in_="91d")
