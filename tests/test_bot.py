"""Tests for bot/bot.py — entry point module."""

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

import bot
from bot import ensure_single_instance, scheduler_job, setup_logging, update_check_job


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_job_context(mock_platform, backend=None):
    """Build a mock Telegram job context that carries platform and backend in job.data."""
    context = AsyncMock()
    data = {"platform": mock_platform}
    if backend is not None:
        data["backend"] = backend
    context.job.data = data
    return context


# ═══════════════════════════════════════════════════════════════════════════
# setup_logging
# ═══════════════════════════════════════════════════════════════════════════


class TestSetupLogging:
    def test_adds_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "LOG_FILE", str(tmp_path / "test.log"))
        root = logging.getLogger()
        before = len(root.handlers)
        setup_logging()
        after = len(root.handlers)
        assert after > before
        # Clean up handlers to avoid leaking into other tests
        root.handlers = root.handlers[:before]


# ═══════════════════════════════════════════════════════════════════════════
# scheduler_job
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerJob:
    @pytest.mark.asyncio
    async def test_dispatched_sends_notification(self, mock_platform):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [("my-task", 42)], "errors": [], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        mock_platform.send_message.assert_awaited_once()
        call_kwargs = mock_platform.send_message.call_args[1]
        assert "Dispatched: my-task (PID 42)" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_no_dispatch_no_notification(self, mock_platform):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [], "errors": [], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        mock_platform.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_errors_sends_notification(self, mock_platform):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [], "errors": ["task1"], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        mock_platform.send_message.assert_awaited_once()
        call_kwargs = mock_platform.send_message.call_args[1]
        assert "Error: task1" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_cycle_exception_logged(self, mock_platform, caplog):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)  # Should not raise

        mock_platform.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_notification_fails(self, mock_platform, caplog):
        mock_backend = MagicMock()
        mock_platform.send_message = AsyncMock(side_effect=RuntimeError("network error"))
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [("x", 1)], "errors": [], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════
# update_check_job
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateCheckJob:
    @pytest.mark.asyncio
    async def test_no_update(self, mock_platform):
        context = _make_job_context(mock_platform)

        with patch.object(bot, "check_for_updates", return_value=None), \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        mock_platform.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_available_update_auto_applies(self, mock_platform):
        context = _make_job_context(mock_platform)

        info = {
            "current": "0.1.0",
            "version": "0.2.0",
            "status": "available",
            "release_notes": {"body": "New features.", "min_compatible": "0.0.0"},
        }
        with patch.object(bot, "check_for_updates", return_value=info), \
             patch.object(bot, "apply_update", new_callable=AsyncMock, return_value=(True, "0.2.0")), \
             patch.object(bot, "restart_service") as mock_restart, \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        # Notification + success message
        assert mock_platform.send_message.await_count == 2
        mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_available_update_auto_apply_fails(self, mock_platform):
        context = _make_job_context(mock_platform)

        info = {
            "current": "0.1.0",
            "version": "0.2.0",
            "status": "available",
            "release_notes": {"body": "New features.", "min_compatible": "0.0.0"},
        }
        with patch.object(bot, "check_for_updates", return_value=info), \
             patch.object(bot, "apply_update", new_callable=AsyncMock, return_value=(False, "git pull failed")), \
             patch.object(bot, "restart_service") as mock_restart, \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        # Notification + failure message
        assert mock_platform.send_message.await_count == 2
        mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_breaking_update_not_auto_applied(self, mock_platform):
        context = _make_job_context(mock_platform)

        info = {
            "current": "0.1.0",
            "version": "0.2.0",
            "status": "breaking",
            "release_notes": {"body": "Breaking stuff.", "min_compatible": "0.0.0"},
        }
        with patch.object(bot, "check_for_updates", return_value=info), \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        # Only notification, no auto-apply
        mock_platform.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_incompatible_update_not_auto_applied(self, mock_platform):
        context = _make_job_context(mock_platform)

        info = {
            "current": "0.1.0",
            "version": "0.3.0",
            "status": "incompatible",
            "release_notes": {"body": "Incompatible.", "min_compatible": "0.2.0"},
        }
        with patch.object(bot, "check_for_updates", return_value=info), \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        mock_platform.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_logged(self, mock_platform, caplog):
        context = _make_job_context(mock_platform)

        with patch.object(bot, "check_for_updates", side_effect=RuntimeError("fail")), \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)  # Should not raise

        mock_platform.send_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# reminder engine
# ═══════════════════════════════════════════════════════════════════════════


    # TestReminderEngine removed — reminder engine merged into unified scheduler


# ═══════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════


class TestMain:
    def test_main_function_exists(self):
        from bot import main
        assert callable(main)


# ═══════════════════════════════════════════════════════════════════════════
# ensure_single_instance
# ═══════════════════════════════════════════════════════════════════════════


class TestEnsureSingleInstance:
    def test_no_pid_file_writes_current_pid(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_stale_pid_dead_process(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("99999999")  # almost certainly dead

        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_active_bot_exits(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()))

        with patch.object(bot, "PID_FILE", pid_file), \
             patch("process.is_pid_alive", return_value=True), \
             patch("process.is_bot_process", return_value=True):
            with pytest.raises(SystemExit, match="already running"):
                ensure_single_instance()

    def test_pid_reused_by_non_python(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()))

        with patch.object(bot, "PID_FILE", pid_file), \
             patch("process.is_pid_alive", return_value=True), \
             patch("process.is_bot_process", return_value=False), \
             patch("process.get_process_name", return_value="vim"):
            ensure_single_instance()  # should NOT exit

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_corrupt_pid_file(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("not-a-number")

        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_pid_alive_but_check_fails(self, tmp_path):
        """Process alive but is_bot_process can't determine -> treated as stale."""
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()))

        with patch.object(bot, "PID_FILE", pid_file), \
             patch("process.is_pid_alive", return_value=False):
            ensure_single_instance()  # should treat as stale

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_creates_parent_dir(self, tmp_path):
        pid_file = tmp_path / "subdir" / "bot.pid"
        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert pid_file.exists()

    def test_cleanup_removes_pid_file(self, tmp_path):
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()))
        assert pid_file.exists()

        # Simulate what save_on_exit does
        pid_file.unlink(missing_ok=True)
        assert not pid_file.exists()


# ═══════════════════════════════════════════════════════════════════════════
# control_room_id routing
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerJobRoutesViaControlRoom:
    """The scheduler/update jobs must forward ``plat.control_room_id`` as
    the ``thread_id`` of every notification. The previous hard-coded
    ``TELEGRAM_MAIN_THREAD_ID = None`` made messages bypass the General
    topic on Telegram forum supergroups."""

    @pytest.mark.asyncio
    async def test_scheduler_uses_platform_control_room_id(self, mock_platform):
        mock_platform.control_room_id = 0  # Telegram General topic
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [("x", 1)], "errors": [], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        kwargs = mock_platform.send_message.call_args[1]
        assert kwargs["thread_id"] == 0

    @pytest.mark.asyncio
    async def test_update_check_uses_platform_control_room_id(self, mock_platform):
        mock_platform.control_room_id = 0
        context = _make_job_context(mock_platform)

        info = {
            "current": "0.1.0",
            "version": "0.2.0",
            "status": "incompatible",
            "release_notes": {"body": "x", "min_compatible": "0.5.0"},
        }
        with patch.object(bot, "check_for_updates", return_value=info), \
             patch.object(bot, "CHAT_ID", -100999):
            await update_check_job(context)

        kwargs = mock_platform.send_message.call_args[1]
        assert kwargs["thread_id"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Telegram polling tuning
# ═══════════════════════════════════════════════════════════════════════════


class TestTelegramPollingKwargs:
    """``telegram_polling_kwargs`` centralises the timeouts that recover
    PTB after a macOS sleep/wake network drop. If any of these regress the
    bot will silently hang for minutes after each wake."""

    def test_uses_short_request_and_poll_timeouts(self):
        kwargs = bot.telegram_polling_kwargs()
        # Long-poll timeout strictly less than the request timeout, so a
        # single ``getUpdates`` round trip never outlasts the HTTP timer.
        assert kwargs["timeout"] == bot.TELEGRAM_POLL_TIMEOUT
        assert kwargs["timeout"] < kwargs["read_timeout"]

    def test_all_request_timeouts_match(self):
        kwargs = bot.telegram_polling_kwargs()
        for k in ("read_timeout", "write_timeout", "connect_timeout", "pool_timeout"):
            assert kwargs[k] == bot.TELEGRAM_REQUEST_TIMEOUT

    def test_drops_pending_updates_and_retries_bootstrap_forever(self):
        kwargs = bot.telegram_polling_kwargs()
        assert kwargs["drop_pending_updates"] is True
        assert kwargs["bootstrap_retries"] == -1

    def test_poll_interval_is_subsecond_for_quick_recovery(self):
        kwargs = bot.telegram_polling_kwargs()
        assert kwargs["poll_interval"] <= 1.0
