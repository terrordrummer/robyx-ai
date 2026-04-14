"""Tests for bot/process.py — cross-platform process utilities.

Two code paths per operation:

- ``*_sync`` variants are used by startup code that runs **before** the
  event loop exists (``_bootstrap.py``).
- async variants are used by every other caller and must not block the
  event loop.

Both are tested here so regressions in either surface immediately.
"""

import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── is_pid_alive ──────────────────────────────────────────────────────────


class TestIsPidAlive:
    def test_current_process_is_alive(self):
        from process import is_pid_alive

        assert is_pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        from process import is_pid_alive

        # PID 99999999 is very unlikely to exist
        assert is_pid_alive(99999999) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    def test_os_kill_oserror_returns_false(self):
        from process import is_pid_alive

        with patch("os.kill", side_effect=OSError("no such process")):
            assert is_pid_alive(12345) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
    def test_win32_timeout_returns_false(self):
        from process import is_pid_alive

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tasklist", 5)):
            assert is_pid_alive(12345) is False


# ── get_process_name_sync (pre-event-loop startup code) ──────────────────


class TestGetProcessNameSync:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    def test_returns_current_process_name(self):
        from process import get_process_name_sync

        name = get_process_name_sync(os.getpid())
        assert isinstance(name, str)
        assert "python" in name or name == ""

    def test_returns_empty_on_timeout(self):
        from process import get_process_name_sync

        # On Linux the /proc shortcut is tried first and may succeed; mock
        # Path.exists to False to force the subprocess branch.
        with patch("process.Path") as MockPath, \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            MockPath.return_value.exists.return_value = False
            assert get_process_name_sync(12345) == ""

    def test_returns_empty_on_oserror(self):
        from process import get_process_name_sync

        with patch("process.Path") as MockPath, \
             patch("subprocess.run", side_effect=OSError("not found")):
            MockPath.return_value.exists.return_value = False
            assert get_process_name_sync(12345) == ""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    def test_returns_empty_for_nonexistent_pid(self):
        from process import get_process_name_sync

        name = get_process_name_sync(99999999)
        assert name == ""


# ── get_process_name (async, event-loop friendly) ────────────────────────


def _mock_proc(stdout_bytes: bytes = b"", stderr_bytes: bytes = b""):
    """Build an AsyncMock replica of ``asyncio.subprocess.Process``."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout_bytes, stderr_bytes))
    return proc


class TestGetProcessName:
    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    async def test_returns_current_process_name(self):
        from process import get_process_name

        name = await get_process_name(os.getpid())
        assert isinstance(name, str)
        assert "python" in name or name == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self):
        import asyncio

        from process import get_process_name

        async def _raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_raise_timeout), \
             patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_mock_proc())):
            assert await get_process_name(12345) == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_oserror(self):
        from process import get_process_name

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("not found"))):
            assert await get_process_name(12345) == ""

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    async def test_returns_empty_for_nonexistent_pid(self):
        from process import get_process_name

        name = await get_process_name(99999999)
        assert name == ""


# ── is_bot_process / is_ai_process (async) ───────────────────────────────


class TestIsBotProcess:
    @pytest.mark.asyncio
    async def test_python_process_is_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", new=AsyncMock(return_value="python3.10")):
            assert await is_bot_process(1234) is True

    @pytest.mark.asyncio
    async def test_non_python_is_not_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", new=AsyncMock(return_value="nginx")):
            assert await is_bot_process(1234) is False

    @pytest.mark.asyncio
    async def test_empty_name_is_not_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", new=AsyncMock(return_value="")):
            assert await is_bot_process(1234) is False


class TestIsAiProcess:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["claude", "codex", "opencode", "python", "node"])
    async def test_ai_process_names(self, name):
        from process import is_ai_process

        with patch("process.get_process_name", new=AsyncMock(return_value=name)):
            assert await is_ai_process(1234) is True

    @pytest.mark.asyncio
    async def test_non_ai_process(self):
        from process import is_ai_process

        with patch("process.get_process_name", new=AsyncMock(return_value="nginx")):
            assert await is_ai_process(1234) is False

    @pytest.mark.asyncio
    async def test_partial_match(self):
        from process import is_ai_process

        with patch("process.get_process_name", new=AsyncMock(return_value="/usr/bin/claude-code")):
            assert await is_ai_process(1234) is True


# ── is_bot_process_sync ──────────────────────────────────────────────────


class TestIsBotProcessSync:
    def test_python_process_is_bot(self):
        from process import is_bot_process_sync

        with patch("process.get_process_name_sync", return_value="python3.10"):
            assert is_bot_process_sync(1234) is True

    def test_non_python_is_not_bot(self):
        from process import is_bot_process_sync

        with patch("process.get_process_name_sync", return_value="nginx"):
            assert is_bot_process_sync(1234) is False
