"""Tests for bot/process.py — cross-platform process utilities."""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

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


# ── get_process_name ──────────────────────────────────────────────────────


class TestGetProcessName:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    def test_returns_current_process_name(self):
        from process import get_process_name

        name = get_process_name(os.getpid())
        assert isinstance(name, str)
        # The current process is a Python process
        assert "python" in name or name == ""

    def test_returns_empty_on_timeout(self):
        from process import get_process_name

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            assert get_process_name(12345) == ""

    def test_returns_empty_on_oserror(self):
        from process import get_process_name

        with patch("subprocess.run", side_effect=OSError("not found")):
            assert get_process_name(12345) == ""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
    def test_returns_empty_for_nonexistent_pid(self):
        from process import get_process_name

        name = get_process_name(99999999)
        assert name == ""


# ── is_bot_process ────────────────────────────────────────────────────────


class TestIsBotProcess:
    def test_python_process_is_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", return_value="python3.10"):
            assert is_bot_process(1234) is True

    def test_non_python_is_not_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", return_value="nginx"):
            assert is_bot_process(1234) is False

    def test_empty_name_is_not_bot(self):
        from process import is_bot_process

        with patch("process.get_process_name", return_value=""):
            assert is_bot_process(1234) is False


# ── is_ai_process ─────────────────────────────────────────────────────────


class TestIsAiProcess:
    @pytest.mark.parametrize("name", ["claude", "codex", "opencode", "python", "node"])
    def test_ai_process_names(self, name):
        from process import is_ai_process

        with patch("process.get_process_name", return_value=name):
            assert is_ai_process(1234) is True

    def test_non_ai_process(self):
        from process import is_ai_process

        with patch("process.get_process_name", return_value="nginx"):
            assert is_ai_process(1234) is False

    def test_partial_match(self):
        from process import is_ai_process

        with patch("process.get_process_name", return_value="/usr/bin/claude-code"):
            assert is_ai_process(1234) is True
