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
        original_handlers = list(root.handlers)
        try:
            setup_logging()
            added_handlers = [
                handler for handler in root.handlers
                if handler not in original_handlers
            ]
            assert added_handlers
        finally:
            # Removing a FileHandler does not close its underlying stream.
            # Close only the handlers created by this test and restore the
            # original logging topology even when the assertion fails.
            for handler in list(root.handlers):
                if handler not in original_handlers:
                    root.removeHandler(handler)
                    handler.close()


# ═══════════════════════════════════════════════════════════════════════════
# scheduler_job
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerJob:
    @pytest.mark.asyncio
    async def test_dispatched_is_not_pushed_to_hq(self, mock_platform):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [("my-task", 42)], "errors": [], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        mock_platform.send_message.assert_not_awaited()

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
    async def test_errors_are_not_pushed_to_hq(self, mock_platform):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        result = {"dispatched": [], "errors": ["task1"], "skipped": []}
        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, return_value=result), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)

        mock_platform.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cycle_exception_logged(self, mock_platform, caplog):
        mock_backend = MagicMock()
        context = _make_job_context(mock_platform, backend=mock_backend)

        with patch.object(bot, "run_scheduler_cycle", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch.object(bot, "CHAT_ID", -100999):
            await scheduler_job(context)  # Should not raise

        mock_platform.send_message.assert_not_awaited()

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


@pytest.fixture
def _reset_pid_lock():
    """Reset the module-level lock fd before/after each test to avoid
    leaking file descriptors across tests."""
    import os as _os
    bot._PID_LOCK_FD = None
    yield
    if bot._PID_LOCK_FD is not None:
        try:
            _os.close(bot._PID_LOCK_FD)
        except OSError:
            pass
        bot._PID_LOCK_FD = None


class TestEnsureSingleInstance:
    def test_no_pid_file_writes_current_pid(self, tmp_path, _reset_pid_lock):
        pid_file = tmp_path / "bot.pid"
        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_stale_pid_dead_process(self, tmp_path, _reset_pid_lock):
        """A PID file left behind by a crashed process is overwritten —
        the kernel-held flock is gone so we acquire cleanly."""
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("99999999")  # almost certainly dead

        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_active_bot_exits(self, tmp_path, _reset_pid_lock):
        """When another process holds the lockfile, we exit with the
        owner's PID in the message. Simulated by mocking flock to raise."""
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("4242")

        import fcntl as _fcntl

        def _raise_blocking(fd, op):
            raise BlockingIOError("lock held")

        with patch.object(bot, "PID_FILE", pid_file), \
             patch.object(_fcntl, "flock", side_effect=_raise_blocking):
            with pytest.raises(SystemExit, match="already running.*4242"):
                ensure_single_instance()

    def test_active_bot_exits_without_pid_file(self, tmp_path, _reset_pid_lock):
        """Lock held but PID file missing or unreadable — still exit, with
        a generic message."""
        pid_file = tmp_path / "bot.pid"
        # no pid file written
        import fcntl as _fcntl

        def _raise_blocking(fd, op):
            raise BlockingIOError("lock held")

        with patch.object(bot, "PID_FILE", pid_file), \
             patch.object(_fcntl, "flock", side_effect=_raise_blocking):
            with pytest.raises(SystemExit, match="already running"):
                ensure_single_instance()

    def test_lock_fd_retained_for_process_lifetime(self, tmp_path, _reset_pid_lock):
        """After successful acquisition, the lock fd is stored globally so
        the kernel keeps the lock until the process exits."""
        pid_file = tmp_path / "bot.pid"
        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert bot._PID_LOCK_FD is not None
        assert isinstance(bot._PID_LOCK_FD, int)

    def test_corrupt_pid_file(self, tmp_path, _reset_pid_lock):
        """Garbage in the existing PID file doesn't block startup when the
        lock is free."""
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text("not-a-number")

        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_creates_parent_dir(self, tmp_path, _reset_pid_lock):
        pid_file = tmp_path / "subdir" / "bot.pid"
        with patch.object(bot, "PID_FILE", pid_file):
            ensure_single_instance()

        assert pid_file.exists()

    def test_non_posix_fallback_uses_process_checks(self, tmp_path, _reset_pid_lock, monkeypatch):
        """When fcntl is unavailable (Windows), fall back to the legacy
        PID-file inspection path — still correct in its constrained use
        case even though it carries a TOCTOU race window."""
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(os.getpid()))

        # Force the import inside ensure_single_instance to fail.
        import builtins
        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "fcntl":
                raise ImportError("no fcntl on this platform")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        with patch.object(bot, "PID_FILE", pid_file), \
             patch("process.is_pid_alive", return_value=True), \
             patch("process.is_bot_process_sync", return_value=True):
            with pytest.raises(SystemExit, match="already running"):
                ensure_single_instance()

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


class TestUpdateJobRoutesViaControlRoom:
    """Update notices use the platform control room; scheduler cycles are silent."""

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


class TestTelegramCommandFallback:
    @pytest.mark.asyncio
    async def test_unknown_slash_reaches_shared_message_handler(self, monkeypatch):
        """PTB must not drop collaborative commands excluded from COMMANDS."""

        class FakeJobQueue:
            run_repeating = MagicMock()
            run_once = MagicMock()

        class FakeApplication:
            def __init__(self):
                self.bot = MagicMock()
                self.job_queue = FakeJobQueue()
                self.handlers = []
                self.run_polling = MagicMock()

            def add_handler(self, handler, group=0):
                self.handlers.append((group, handler))

        app = FakeApplication()

        class FakeBuilder:
            def token(self, _value):
                return self

            def concurrent_updates(self, _value):
                return self

            def post_shutdown(self, _callback):
                return self

            def build(self):
                return app

        monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
        monkeypatch.setattr(bot, "voice_available", lambda: False)

        platform = MagicMock()
        platform.control_room_id = 0
        platform.register_lifecycle = MagicMock()
        platform.set_bot = MagicMock()
        shared_message = AsyncMock()
        handlers = {
            name: AsyncMock()
            for name in (
                "start", "help", "workspaces", "specialists", "status",
                "reset", "clear", "focus", "ping", "checkupdate",
                "doupdate", "stop", "resume", "complete", "delete",
                "voice",
            )
        }
        handlers["message"] = shared_message

        bot._run_telegram(platform, handlers, MagicMock(), MagicMock())

        assert callable(app.post_init)

        fallback = [
            handler
            for _group, handler in app.handlers
            if isinstance(handler, bot.MessageHandler)
            and handler.filters is bot.filters.COMMAND
        ]
        assert len(fallback) == 1

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_user.first_name = "Owner"
        update.effective_user.last_name = None
        update.effective_user.username = None
        update.effective_chat.id = -200111
        update.message.text = "/promote 99999"
        update.message.message_thread_id = None
        await fallback[0].callback(update, MagicMock())

        shared_message.assert_awaited_once()
        routed = shared_message.await_args.args[1]
        assert routed.text == "/promote 99999"
        assert routed.chat_id == -200111


class TestBootMigrationBoundary:
    @pytest.mark.asyncio
    async def test_corrupt_migration_tracker_aborts_boot(self, monkeypatch):
        from persistence_recovery import PersistenceUnavailableError

        platform = AsyncMock()
        monkeypatch.setattr(bot, "migrate_to_unified_queue", lambda: 0)
        monkeypatch.setattr(
            bot,
            "run_pending_migrations",
            AsyncMock(side_effect=PersistenceUnavailableError("tracker corrupt")),
        )

        with pytest.raises(PersistenceUnavailableError, match="tracker corrupt"):
            await bot._run_boot_sequence(platform, MagicMock(), 1)

        platform.send_message.assert_not_awaited()
