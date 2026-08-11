#!/usr/bin/env python3
"""
Robyx — AI-powered agent staff, orchestrated through a messaging platform.

Architecture:
- Robyx (Principal Orchestrator): lives on the Headquarters (the control channel/topic)
- Workspace Agents: one per topic, dedicated to a specific task/project
- Cross-functional Specialists: horizontal experts available across workspaces
- Scheduler Agent: runs every N minutes, activates periodic and one-shot tasks
- AI Backend: pluggable (Claude Code, Codex, OpenCode)
"""

# MUST run before any other bot-local import: keeps the venv in sync with
# the hash-verified runtime lock for this Python minor so a new release with
# new deps never boots against a stale environment. See bot/_bootstrap.py.
import _bootstrap
_bootstrap.ensure_dependencies()
# v0.16+: relocate any leftover repo-root runtime files into data/. This
# is the boot-time safety net that complements the pre-pull migration in
# bot/updater.py — see migrate_personal_data_if_needed() for rationale.
_bootstrap.migrate_personal_data_if_needed()

# Enforce owner-only modes before ``config`` reads .env.  Kept behind the
# executable-entrypoint check so importing this module does not alter a test
# runner's process-wide umask or touch its filesystem.
if __name__ == "__main__":
    from local_security import (
        emit_hardening_report,
        establish_private_umask,
        harden_runtime_permissions,
        PermissionHardeningError,
        require_hardening_success,
    )

    establish_private_umask()
    _permission_report = harden_runtime_permissions(_bootstrap._PROJECT_ROOT)
    emit_hardening_report(_permission_report)
    try:
        require_hardening_success(_permission_report)
    except PermissionHardeningError as _permission_error:
        # A concise SystemExit avoids a traceback and never embeds runtime
        # contents; detailed path-only diagnostics were emitted just above.
        raise SystemExit(str(_permission_error)) from None

import asyncio
import atexit
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from agents import AgentManager
from ai_backend import create_backend
from config import (
    AI_BACKEND,
    AI_CLI_PATH,
    BOT_TOKEN,
    CHAT_ID,
    DATA_DIR,
    DISCORD_BOT_TOKEN,
    DISCORD_CONTROL_CHANNEL_ID,
    DISCORD_GUILD_ID,
    DISCORD_OWNER_ID,
    LOG_FILE,
    OWNER_ID,
    PLATFORM,
    SCHEDULER_INTERVAL,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_OWNER_ID,
    UPDATE_CHECK_INTERVAL,
)
from handlers import make_handlers
from messaging.base import PlatformMessage
from migrations import run_pending as run_pending_migrations
from persistence_recovery import PersistenceUnavailableError
from runtime_supervisor import get_runtime_supervisor
from scheduler import migrate_to_unified_queue, run_scheduler_cycle
from topics import heal_detached_workspaces
from updater import apply_update, check_for_updates, get_pending_update, restart_service
from voice import is_available as voice_available

log = logging.getLogger("robyx")

PID_FILE = DATA_DIR / "bot.pid"

# Holds the file descriptor that owns the single-instance lock for the life
# of the process. Closing the fd (or the process exiting) releases the lock.
_PID_LOCK_FD: int | None = None

# ── Telegram polling tuning ────────────────────────────────────────────────
#
# Default PTB long-polling can hang for minutes after a macOS sleep/wake
# cycle: the underlying TCP connection is silently dead, but neither side
# detects it until a write actually fails. The bot then looks "alive" but
# never answers until it eventually times out and reconnects.
#
# We work around it by capping every Telegram-side timeout at ~15 seconds
# and asking PTB to retry the bootstrap forever. The bot recovers within
# one polling cycle of the wake event instead of the user noticing.
TELEGRAM_POLL_TIMEOUT = 10
TELEGRAM_REQUEST_TIMEOUT = 15


def telegram_polling_kwargs() -> dict:
    """Return the kwargs we pass to ``Application.run_polling``.

    Centralised so the same configuration is used in production and in any
    future test that exercises the start path.
    """
    return {
        "drop_pending_updates": True,
        "allowed_updates": Update.ALL_TYPES,
        "bootstrap_retries": -1,
        "poll_interval": 1.0,
        "timeout": TELEGRAM_POLL_TIMEOUT,
        "read_timeout": TELEGRAM_REQUEST_TIMEOUT,
        "write_timeout": TELEGRAM_REQUEST_TIMEOUT,
        "connect_timeout": TELEGRAM_REQUEST_TIMEOUT,
        "pool_timeout": TELEGRAM_REQUEST_TIMEOUT,
    }


def ensure_single_instance():
    """Verify no other bot instance is running. Write PID file for current process.

    Uses a POSIX ``fcntl.LOCK_EX | LOCK_NB`` advisory lock on a sidecar
    lockfile. The kernel releases the lock automatically when the owning
    process exits, so stale PID files never leave the lockfile held.

    On platforms without ``fcntl`` (Windows), falls back to the legacy
    PID-file inspection (TOCTOU-susceptible but the Windows service
    manager makes concurrent starts unlikely in practice).
    """
    global _PID_LOCK_FD

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl  # POSIX only
    except ImportError:
        fcntl = None

    if fcntl is not None:
        lock_path = PID_FILE.with_suffix(PID_FILE.suffix + ".lock")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            try:
                owner_pid = int(PID_FILE.read_text().strip())
                sys.exit("Robyx already running (PID %d)" % owner_pid)
            except (OSError, ValueError):
                sys.exit("Robyx already running (another instance holds the lock)")
        _PID_LOCK_FD = fd
    else:
        # Windows fallback: no fcntl, so we can't hold a real file lock
        # for the life of the process. Instead we consult the PID file and
        # check whether the recorded process looks like another running
        # bot. There is a narrow TOCTOU window between the liveness check
        # and ``PID_FILE.write_text`` below where two concurrent starts
        # could both decide they are the sole instance. This is an
        # accepted trade-off — Windows deployments are rare in practice
        # and the POSIX path above is race-free. If this ever becomes a
        # real problem, switch to ``os.open(..., O_CREAT | O_EXCL)`` for
        # atomic exclusive creation.
        from process import is_pid_alive, is_bot_process_sync, get_process_name_sync

        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                if is_pid_alive(pid):
                    if is_bot_process_sync(pid):
                        sys.exit("Robyx already running (PID %d)" % pid)
                    proc_name = get_process_name_sync(pid)
                    log.warning("Stale PID file: PID %d is now '%s'. Overwriting.", pid, proc_name)
                else:
                    log.warning("Stale PID file found. Overwriting.")
            except ValueError:
                log.warning("Corrupt PID file found. Overwriting.")

    PID_FILE.write_text(str(os.getpid()))


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


async def scheduler_job(context):
    """Job: run one unified scheduler cycle (tasks + reminders)."""
    backend = context.job.data["backend"]
    plat = context.job.data["platform"]
    control_room_id = plat.control_room_id

    try:
        result = await run_scheduler_cycle(
            backend,
            platform=plat,
            default_chat_id=context.job.data.get("default_chat_id"),
        )

        # Spec 006: scheduler activity is pull-based. Per-task delivery and the
        # event journal carry actionable outcomes; routine dispatch/error
        # summaries must never leak into HQ.
        if result["dispatched"] or result["errors"]:
            log.info(
                "Scheduler cycle summary: dispatched=%d errors=%d",
                len(result["dispatched"]),
                len(result["errors"]),
            )

    except Exception as e:
        log.error("Scheduler cycle failed: %s", e, exc_info=True)


async def update_check_job(context):
    """Job: check for new releases. Auto-apply safe updates, notify for breaking/incompatible."""
    from i18n import STRINGS

    plat = context.job.data["platform"]
    manager = context.job.data.get("manager")
    control_room_id = plat.control_room_id
    log.info("Update check: running")
    try:
        info = await check_for_updates()
        if not info:
            return

        notes = info["release_notes"]
        body = notes["body"].strip() if notes else "(no release notes)"

        if info["status"] == "incompatible":
            text = STRINGS["update_available_incompatible"] % (
                info["current"], info["version"], notes["min_compatible"],
            )
            await plat.send_message(
                chat_id=CHAT_ID, text=text, thread_id=control_room_id, parse_mode="markdown",
            )
            return

        if info["status"] == "breaking":
            text = STRINGS["update_available_breaking"] % (
                info["current"], info["version"], body, info["version"],
            )
            await plat.send_message(
                chat_id=CHAT_ID, text=text, thread_id=control_room_id, parse_mode="markdown",
            )
            return

        # Safe update — auto-apply
        version = info["version"]
        log.info("Auto-update: applying v%s", version)

        await plat.send_message(
            chat_id=CHAT_ID,
            text=STRINGS["update_auto_applying"] % (info["current"], version, body),
            thread_id=control_room_id, parse_mode="markdown",
        )

        success, result = await apply_update(version, manager=manager)

        if success:
            await plat.send_message(
                chat_id=CHAT_ID,
                text=STRINGS["update_success"] % result,
                thread_id=control_room_id, parse_mode="markdown",
            )
            restart_service()
        else:
            await plat.send_message(
                chat_id=CHAT_ID,
                text=STRINGS["update_auto_failed"] % (version, result),
                thread_id=control_room_id, parse_mode="markdown",
            )
    except Exception as e:
        log.error("Update check failed: %s", e, exc_info=True)




def main():
    setup_logging()
    ensure_single_instance()
    log.info("Starting Robyx...")

    # Force-kill any subprocess the previous bot lifetime left behind
    # (interrupted-but-not-confirmed SIGTERM recipients, etc.). Safe to
    # run before backend init — it only touches PIDs we recorded.
    try:
        import orphan_tracker
        killed = orphan_tracker.cleanup_on_startup()
        if killed:
            log.warning("Startup: killed %d orphan subprocess(es): %s", len(killed), killed)
    except Exception as exc:
        log.error("Orphan cleanup failed: %s", exc, exc_info=True)

    # Initialize AI backend
    backend = create_backend(AI_BACKEND, AI_CLI_PATH or None)
    log.info("AI backend: %s (%s)", backend.name, backend.cli_path)

    # Initialize agent manager
    manager = AgentManager()

    # Create platform
    if PLATFORM == "telegram":
        if not (BOT_TOKEN and CHAT_ID and OWNER_ID):
            raise RuntimeError(
                "Telegram platform requires ROBYX_BOT_TOKEN, ROBYX_CHAT_ID, "
                "and ROBYX_OWNER_ID in .env"
            )
        from messaging.telegram import TelegramPlatform
        plat = TelegramPlatform(BOT_TOKEN, CHAT_ID, OWNER_ID)
    elif PLATFORM == "slack":
        from messaging.slack import SlackPlatform
        plat = SlackPlatform(SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, SLACK_OWNER_ID)
    elif PLATFORM == "discord":
        from messaging.discord import DiscordPlatform
        plat = DiscordPlatform(
            DISCORD_BOT_TOKEN,
            DISCORD_GUILD_ID,
            DISCORD_OWNER_ID,
            DISCORD_CONTROL_CHANNEL_ID,
        )
    else:
        raise ValueError("Unsupported platform: %s" % PLATFORM)

    _shutdown_done = False

    def save_on_exit(*_args):
        nonlocal _shutdown_done
        if _shutdown_done:
            return
        _shutdown_done = True
        log.info("Shutting down — saving state...")
        try:
            # Normal adapter shutdown drains asynchronously.  This bounded
            # fallback also covers SIGTERM/atexit paths where the event loop
            # has already stopped or cannot be awaited.
            get_runtime_supervisor().terminate_processes_sync()
            manager.save_state()
        finally:
            PID_FILE.unlink(missing_ok=True)

    atexit.register(save_on_exit)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *a: (save_on_exit(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *a: (save_on_exit(), sys.exit(0)))

    # Initialize collaborative workspace store
    from collaborative import CollabStore
    collab_store = CollabStore()

    # Expose the store to the orchestrator system-prompt builder so the
    # live [AVAILABLE_EXTERNAL_GROUPS] section is rendered every turn
    # (feature 003-external-group-wiring, R-04).
    from ai_invoke import register_collab_store
    register_collab_store(collab_store)

    # Build handlers
    h = make_handlers(manager, backend, collab_store=collab_store)

    if PLATFORM == "slack":
        _run_slack(plat, h, backend, manager)
    elif PLATFORM == "discord":
        _run_discord(plat, h, backend, manager)
    else:
        _run_telegram(plat, h, backend, manager)


def _wire_lifecycle(plat, h) -> None:
    """Spec 007: register the platform-agnostic collaborative-workspace
    lifecycle callbacks on ``plat`` so every adapter dispatches through
    the same handlers regardless of class.

    Adapters that emit lifecycle events (Telegram, Discord post-007,
    Slack post-008) invoke ``plat.on_added`` / ``on_removed`` /
    ``on_migrated`` with the platform-agnostic event dataclasses; the
    handlers in ``bot.handlers`` branch on ``event.chat_ref.platform``
    rather than the adapter class. Adapters that do not yet emit (Slack
    in 007) leave their native event handler unregistered — the
    callbacks stay set but are simply never fired.

    Each runner calls the adapter's native lifecycle registration after this
    shared callback wiring; Telegram and Discord currently emit events, with
    Slack support supplied by its adapter lifecycle bridge.
    """
    if "collab_bot_added" in h:
        plat.on_added = lambda evt: h["collab_bot_added"](plat, evt)
    if "collab_bot_removed" in h:
        plat.on_removed = lambda evt: h["collab_bot_removed"](plat, evt)
    if "collab_bot_migrated" in h:
        plat.on_migrated = lambda evt: h["collab_bot_migrated"](plat, evt)


def _run_telegram(plat, h, backend, manager):
    """Start the Telegram event loop."""
    supervisor = get_runtime_supervisor()

    async def _post_shutdown(_application):
        await supervisor.shutdown()

    # Build Telegram application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Give platform access to the bot instance
    plat.set_bot(app.bot)

    async def _log_raw_update(update, context):
        if not isinstance(update, Update):
            return
        msg = update.effective_message
        log.info(
            "Telegram raw update: kind=%s chat=%s user=%s thread=%s chars=%d",
            "message" if update.message else "other",
            getattr(update.effective_chat, "id", None),
            getattr(update.effective_user, "id", None),
            getattr(msg, "message_thread_id", None),
            len(getattr(msg, "text", "") or ""),
        )

    app.add_handler(TypeHandler(Update, _log_raw_update), group=-1)

    # Wrap handlers to bridge Telegram's (update, context) to our (platform, msg, msg_ref)
    def _get_user_display_name(user) -> str | None:
        if user is None:
            return None
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or user.username or str(user.id)

    def _wrap_command(handler_fn):
        async def wrapper(update, context):
            log.info(
                "Telegram command received: user=%s chat=%s thread=%s chars=%d",
                getattr(update.effective_user, "id", None),
                getattr(update.effective_chat, "id", None),
                getattr(update.message, "message_thread_id", None),
                len(getattr(update.message, "text", "") or ""),
            )
            msg = PlatformMessage(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                text=update.message.text,
                thread_id=getattr(update.message, "message_thread_id", None),
                args=context.args or [],
                user_name=_get_user_display_name(update.effective_user),
            )
            await handler_fn(plat, msg, update.message)
        return wrapper

    def _wrap_message(handler_fn):
        async def wrapper(update, context):
            log.info(
                "Telegram text received: user=%s chat=%s thread=%s chars=%d",
                getattr(update.effective_user, "id", None),
                getattr(update.effective_chat, "id", None),
                getattr(update.message, "message_thread_id", None),
                len(getattr(update.message, "text", "") or ""),
            )
            msg = PlatformMessage(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                text=update.message.text,
                thread_id=getattr(update.message, "message_thread_id", None),
                user_name=_get_user_display_name(update.effective_user),
            )
            await handler_fn(plat, msg, update.message)
        return wrapper

    def _wrap_voice(handler_fn):
        async def wrapper(update, context):
            voice = update.message.voice or update.message.audio
            log.info(
                "Telegram voice received: user=%s chat=%s thread=%s file=%s",
                getattr(update.effective_user, "id", None),
                getattr(update.effective_chat, "id", None),
                getattr(update.message, "message_thread_id", None),
                getattr(voice, "file_id", None),
            )
            msg = PlatformMessage(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                text=None,
                thread_id=getattr(update.message, "message_thread_id", None),
                voice_file_id=voice.file_id if voice else None,
            )
            await handler_fn(plat, msg, update.message)
        return wrapper

    # Register command handlers
    for name in ("start", "help", "workspaces", "specialists", "status", "reset", "clear", "focus", "ping", "checkupdate", "doupdate", "stop", "resume", "complete", "delete"):
        app.add_handler(CommandHandler(name, _wrap_command(h[name])))

    # PTB excludes every slash-prefixed message from the ordinary text
    # handler. Keep a fallback after the known CommandHandlers so
    # collaborative commands such as /promote, /demote, /role, /mode and
    # /close still cross the shared role-aware handler boundary. Known HQ
    # commands win because PTB uses the first matching handler in a group.
    app.add_handler(MessageHandler(filters.COMMAND, _wrap_message(h["message"])))

    # Register voice handler (always — replies gracefully if Whisper not configured)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _wrap_voice(h["voice"])))
    log.info("Voice transcription: %s", "enabled" if voice_available() else "disabled (no OPENAI_API_KEY)")

    # Register text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _wrap_message(h["message"])))

    # Spec 007: lifecycle dispatch is now adapter-side. The
    # TelegramPlatform.register_lifecycle method registers the PTB
    # ChatMemberHandler and translates updates into platform-agnostic
    # LifecycleAdded / LifecycleRemoved / LifecycleMigrated events.
    _wire_lifecycle(plat, h)
    plat.register_lifecycle(app)

    # Unified scheduler: runs every SCHEDULER_INTERVAL seconds (default 60s).
    # Handles periodic tasks, one-shot tasks, reminders, and continuous tasks.
    log.info("Unified scheduler: interval=%ds", SCHEDULER_INTERVAL)
    app.job_queue.run_repeating(
        scheduler_job,
        interval=SCHEDULER_INTERVAL,
        first=5,  # First run 5 seconds after startup (after migration)
        data={"backend": backend, "platform": plat, "default_chat_id": CHAT_ID},
    )

    # Update checker: runs every UPDATE_CHECK_INTERVAL seconds.
    log.info("Update checker: interval=%ds", UPDATE_CHECK_INTERVAL)
    app.job_queue.run_repeating(
        update_check_job,
        interval=UPDATE_CHECK_INTERVAL,
        first=30,  # First check 30 seconds after startup
        data={"platform": plat, "manager": manager},
    )

    # Run data recovery/migrations in PTB's ``post_init`` hook, before the
    # updater starts polling or the JobQueue starts scheduler/update work. A
    # fail-closed persistence error therefore aborts process startup instead
    # of leaving a partially online Telegram bot.
    async def boot_notify(_application):
        from updater import get_current_version
        boot_plat = plat
        boot_manager = manager
        control_room_id = boot_plat.control_room_id

        # Heal any workspaces whose Telegram topic was lost between
        # restarts (Thread ID = "-" in tasks.md). Done before the boot
        # message so the summary reflects the freshly-attached channels.
        # NOTE: prior to v0.15.2, this referenced ``manager`` from the
        # surrounding closure, which was not in scope inside
        # ``_run_telegram`` — every boot since v0.14 silently raised a
        # NameError here, swallowed by the except. We now thread the
        # AgentManager through the job data so the call actually works.
        try:
            repaired = await heal_detached_workspaces(boot_manager, platform=boot_plat)
            if repaired:
                names = ", ".join(r["name"] for r in repaired)
                log.info("Healed detached Telegram workspaces: %s", names)
        except Exception as e:
            log.error("heal_detached_workspaces failed: %s", e, exc_info=True)

        # Spec 006 — ensure event-journal directory exists before any
        # scheduler cycle tries to append. Idempotent; safe on every boot.
        try:
            from config import EVENTS_DIR, EVENTS_HOT_FILE
            EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            if not EVENTS_HOT_FILE.exists():
                EVENTS_HOT_FILE.touch()
        except Exception as e:
            log.error("events dir/file init failed: %s", e, exc_info=True)

        # Migrate legacy scheduler data (tasks.md, timed_queue.json, reminders.json)
        # into the unified queue.json — idempotent, skips if queue.json exists.
        try:
            n_migrated = migrate_to_unified_queue()
            if n_migrated:
                log.info("Boot: migrated %d entries to unified queue", n_migrated)
        except Exception as e:
            log.error("Unified queue migration failed: %s", e, exc_info=True)

        # Run pending migrations (rename channels, state patches, etc.)
        # BEFORE the boot message goes out — that way the boot message lands
        # in the correctly-named channel and migrations that fail are
        # visible in the boot summary. Pass the manager so state-mutating
        # migrations can use ``manager.reset_sessions(...)`` instead of
        # touching ``state.json`` directly (which would be clobbered by
        # the running bot's next ``save_state()`` call).
        try:
            executed = await run_pending_migrations(boot_plat, boot_manager)
        except PersistenceUnavailableError:
            # A corrupt migration tracker makes schema progress unknowable.
            # Continuing could run new code against partially migrated data;
            # fail startup without overwriting or reseeding the tracker.
            raise
        except Exception as e:
            log.error("Migration runner crashed: %s", e, exc_info=True)
            executed = []

        try:
            boot_text = "*Robyx v%s* — Headquarters online." % get_current_version()
            if executed:
                lines = [
                    "- %s → %s" % (mid, status) for mid, status in executed
                ]
                boot_text += "\n\n_Migrations applied:_\n" + "\n".join(lines)
            await boot_plat.send_message(
                chat_id=CHAT_ID,
                text=boot_text,
                thread_id=control_room_id,
                parse_mode="markdown",
            )

            me = await app.bot.get_me()
            if not getattr(me, "can_read_all_group_messages", True):
                warning = (
                    "Telegram privacy mode is still enabled, so I will not receive normal group messages.\n\n"
                    "Disable it in BotFather: `/mybots` -> select your bot -> *Bot Settings* -> "
                    "*Group Privacy* -> *Turn off*."
                )
                await boot_plat.send_message(
                    chat_id=CHAT_ID,
                    text=warning,
                    thread_id=control_room_id,
                    parse_mode="markdown",
                )
                log.warning("Telegram privacy mode enabled; normal group messages will not be delivered")
        except Exception as e:
            log.warning("Boot notification failed: %s", e)

    app.post_init = boot_notify

    log.info(
        "Robyx is running. Polling for updates (timeout=%ss, request_timeout=%ss)...",
        TELEGRAM_POLL_TIMEOUT,
        TELEGRAM_REQUEST_TIMEOUT,
    )

    # Python 3.13 no longer eagerly creates a default main-thread event
    # loop. PTB still expects one to exist when ``run_polling`` starts, so
    # we make sure there is one before handing off control.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app.run_polling(**telegram_polling_kwargs())


def _run_slack(plat, h, backend, manager):
    """Start the Slack event loop using Socket Mode."""
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    app = AsyncApp(token=SLACK_BOT_TOKEN)

    # Give platform access to the async web client
    plat.set_bot(app.client)

    COMMANDS = ("start", "help", "workspaces", "specialists", "status",
                "reset", "clear", "focus", "ping", "checkupdate", "doupdate",
                "stop", "resume", "complete", "delete")

    @app.event("message")
    async def handle_message(event, say, client):
        """Route incoming Slack messages to Robyx handlers."""
        # Ignore bot messages to avoid loops
        if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
            return

        user_id = event.get("user", "")
        channel = event.get("channel", "")
        text = event.get("text", "") or ""
        thread_ts = event.get("thread_ts")

        # Build msg_ref for reply/edit
        msg_ref = {"channel": channel, "ts": event.get("ts", "")}

        # Check for voice/audio files
        files = event.get("files") or []
        audio_file = None
        for f in files:
            if f.get("subtype") == "audio" or (f.get("mimetype", "").startswith("audio/")):
                audio_file = f
                break

        if audio_file:
            msg = PlatformMessage(
                user_id=user_id,
                chat_id=channel,
                text=None,
                thread_id=thread_ts,
                voice_file_id=audio_file.get("url_private_download", ""),
            )
            await h["voice"](plat, msg, msg_ref)
            return

        # Check for /command prefix
        command = None
        args = []
        if text.startswith("/"):
            parts = text.split()
            cmd_name = parts[0][1:].lower()  # strip leading /
            if cmd_name in COMMANDS:
                command = cmd_name
                args = parts[1:]
                msg = PlatformMessage(
                    user_id=user_id,
                    chat_id=channel,
                    text=text,
                    thread_id=thread_ts,
                    command=command,
                    args=args,
                )
                await h[command](plat, msg, msg_ref)
                return

        # Regular text message
        msg = PlatformMessage(
            user_id=user_id,
            chat_id=channel,
            text=text,
            thread_id=thread_ts,
        )
        await h["message"](plat, msg, msg_ref)

    # Spec 007: scaffold the platform-agnostic lifecycle callbacks so
    # spec 008's Slack ``member_joined_channel`` wiring binds without
    # any change here. The Slack adapter does NOT emit LifecycleAdded /
    # LifecycleRemoved in spec 007 — the legacy notice below remains
    # the user-visible behaviour until spec 008 swaps it for a real
    # lifecycle handler.
    _wire_lifecycle(plat, h)

    # External collaborative groups are Telegram + Discord only for now;
    # Slack remains a documented limitation (spec 008 closes it).
    # When the Slack bot is added to a channel, post a single notice so
    # the add isn't a silent no-op.
    @app.event("member_joined_channel")
    async def _on_member_joined_channel(event, client):
        from i18n import STRINGS
        try:
            auth = await client.auth_test()
            bot_user_id = auth.get("user_id")
        except Exception as e:
            log.warning("Slack auth_test failed in member_joined_channel: %s", e)
            return
        if event.get("user") != bot_user_id:
            return
        channel = event.get("channel", "")
        log.info(
            "collab.unsupported_platform platform=slack channel=%s", channel,
        )
        try:
            await client.chat_postMessage(
                channel=channel,
                text=STRINGS["collab_unsupported_platform_slack"],
            )
        except Exception as e:
            log.warning("Failed to post Slack unsupported-platform notice: %s", e)

    async def _run():
        supervisor = get_runtime_supervisor()
        try:
            auth = await app.client.auth_test()
            team_id = auth.get("team_id")
            if not team_id:
                raise RuntimeError(
                    "Slack auth.test did not return team_id; canonical task "
                    "ownership cannot be established"
                )
            plat.set_team_id(team_id)
            control_room = plat.control_room_channel
            await _run_boot_sequence(plat, manager, control_room)

            supervisor.spawn(
                _background_scheduler_loop(plat, backend, control_room),
                name="slack_scheduler",
                key="slack_scheduler",
            )
            supervisor.spawn(
                _background_update_loop(plat, control_room, manager),
                name="slack_updates",
                key="slack_updates",
            )

            handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
            log.info("Robyx is running on Slack (Socket Mode)...")
            await handler.start_async()
        finally:
            await supervisor.shutdown()

    asyncio.run(_run())


def _run_discord(plat, h, backend, manager):
    """Start the Discord event loop using discord.py."""
    import discord
    from collaborative import make_discord_chat_id

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    client = discord.Client(intents=intents)
    plat.set_bot(client)
    supervisor = get_runtime_supervisor()

    # Spec 007: ``im-the-owner`` is the Discord-only manual-claim escape
    # hatch when the audit-log inviter lookup fails (Forbidden / empty
    # / sustained API errors). Slash on Discord = text command parsed by
    # on_message; the same handler is exposed on Slack post-spec-008.
    COMMANDS = ("start", "help", "workspaces", "specialists", "status",
                "reset", "clear", "focus", "ping", "checkupdate", "doupdate",
                "stop", "resume", "complete", "delete", "im-the-owner")

    # ``on_ready`` can run more than once after gateway reconnects. Retain
    # the one-time boot task and the two long-lived loops so a reconnect
    # neither repeats migrations/boot notices nor doubles scheduler work.
    boot_task = None
    background_tasks = {}

    def _start_background(name, coro_factory):
        task = background_tasks.get(name)
        if task is not None and not task.done():
            return task

        task = supervisor.spawn(
            coro_factory(),
            name="discord_%s" % name,
            key="discord_%s" % name,
        )
        background_tasks[name] = task
        return task

    @client.event
    async def on_ready():
        nonlocal boot_task
        log.info("Discord bot ready: %s", client.user)
        control_room = plat.control_room_id
        if boot_task is None:
            boot_task = supervisor.spawn(
                _run_boot_sequence(plat, manager, control_room),
                name="discord_boot_sequence",
            )
        current_boot = boot_task
        try:
            await current_boot
        except BaseException:
            # A later ready event may retry a failed/cancelled boot. Guard
            # the assignment in case another ready callback replaced it.
            if boot_task is current_boot:
                boot_task = None
            raise

        _start_background(
            "scheduler",
            lambda: _background_scheduler_loop(plat, backend, control_room),
        )
        _start_background(
            "updates",
            lambda: _background_update_loop(plat, control_room, manager),
        )

    # Spec 007: Discord collaborative-workspace lifecycle is now wired.
    # DiscordPlatform.register_lifecycle attaches on_guild_join /
    # on_guild_remove handlers that emit LifecycleAdded / LifecycleRemoved
    # events; _wire_lifecycle binds the platform-agnostic
    # collab_bot_added / collab_bot_removed handlers to them. Closes the
    # spec 003 FR-013 justified violation of Principle I for Discord.
    _wire_lifecycle(plat, h)
    plat.register_lifecycle(client)

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return

        # Discord workspace identity is channel-scoped. Keep the canonical
        # value serialisable on PlatformMessage; ``thread_id`` remains the
        # native numeric delivery target for discord.py operations.
        guild_id = message.guild.id if message.guild else 0
        channel_id = message.channel.id
        chat_id = make_discord_chat_id(guild_id, channel_id)

        # Check for voice message
        audio_attachment = None
        for a in message.attachments:
            if a.content_type and "audio" in a.content_type:
                audio_attachment = a
                break

        if message.flags.voice or audio_attachment:
            msg = PlatformMessage(
                user_id=message.author.id,
                chat_id=chat_id,
                text=None,
                thread_id=channel_id,
                voice_file_id=audio_attachment.url if audio_attachment else None,
            )
            await h["voice"](plat, msg, message)
            return

        text = message.content
        if not text:
            return

        # Check for /command prefix
        if text.startswith("/"):
            parts = text.split()
            cmd_name = parts[0][1:].lower()
            if cmd_name in COMMANDS:
                args = parts[1:]
                msg = PlatformMessage(
                    user_id=message.author.id,
                    chat_id=chat_id,
                    text=text,
                    thread_id=channel_id,
                    command=cmd_name,
                    args=args,
                )
                await h[cmd_name](plat, msg, message)
                return

        # Regular text message
        msg = PlatformMessage(
            user_id=message.author.id,
            chat_id=chat_id,
            text=text,
            thread_id=channel_id,
            user_name=(
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None)
            ),
        )
        await h["message"](plat, msg, message)

    # discord.py owns its event loop inside ``Client.run``.  Wrapping its
    # async close hook ensures our retained tasks and AI children are drained
    # before that loop disappears. Reconnects do not invoke close, so they do
    # not tear down the singleton scheduler/update loops.
    original_close = getattr(client, "close", None)
    if callable(original_close):
        async def _supervised_close():
            await supervisor.shutdown()
            await original_close()

        client.close = _supervised_close

    client.run(DISCORD_BOT_TOKEN)


# ── Shared background loops ───────────────────────────────────────────────
#
# These are used by Slack and Discord runners (Telegram uses PTB's
# job_queue instead). Each loop runs a single concern on a fixed interval,
# catching exceptions so one failure never crashes the event loop.


async def _background_scheduler_loop(
    plat, backend, control_room_id, *, interval: int = SCHEDULER_INTERVAL,
) -> None:
    """Periodically run the unified scheduler cycle and notify on activity."""
    await asyncio.sleep(5)  # Let boot finish first
    while True:
        try:
            result = await run_scheduler_cycle(
                backend, platform=plat, default_chat_id=control_room_id,
            )
            if result["dispatched"] or result["errors"]:
                log.info(
                    "Scheduler cycle summary: dispatched=%d errors=%d",
                    len(result["dispatched"]),
                    len(result["errors"]),
                )
        except Exception as e:
            log.error("Scheduler cycle failed: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def _background_update_loop(
    plat, control_room_id, manager, *, interval: int = UPDATE_CHECK_INTERVAL,
) -> None:
    """Periodically check for updates and auto-apply safe ones."""
    from i18n import STRINGS

    await asyncio.sleep(30)  # First check after 30 seconds
    while True:
        log.info("Update check: running")
        try:
            info = await check_for_updates()
            if info:
                notes = info["release_notes"]
                body = notes["body"].strip() if notes else "(no release notes)"

                if info["status"] == "incompatible":
                    text = STRINGS["update_available_incompatible"] % (
                        info["current"], info["version"], notes["min_compatible"],
                    )
                    await plat.send_message(chat_id=control_room_id, text=text, parse_mode="markdown")
                elif info["status"] == "breaking":
                    text = STRINGS["update_available_breaking"] % (
                        info["current"], info["version"], body, info["version"],
                    )
                    await plat.send_message(chat_id=control_room_id, text=text, parse_mode="markdown")
                else:
                    version = info["version"]
                    log.info("Auto-update: applying v%s", version)
                    await plat.send_message(
                        chat_id=control_room_id,
                        text=STRINGS["update_auto_applying"] % (info["current"], version, body),
                        parse_mode="markdown",
                    )
                    success, result = await apply_update(version, manager=manager)
                    if success:
                        await plat.send_message(
                            chat_id=control_room_id,
                            text=STRINGS["update_success"] % result,
                            parse_mode="markdown",
                        )
                        restart_service()
                    else:
                        await plat.send_message(
                            chat_id=control_room_id,
                            text=STRINGS["update_auto_failed"] % (version, result),
                            parse_mode="markdown",
                        )
        except Exception as e:
            log.error("Update check failed: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def _run_boot_sequence(plat, manager, control_room_id) -> list:
    """Run one-time boot tasks shared across Slack and Discord."""
    from updater import get_current_version

    try:
        n_migrated = migrate_to_unified_queue()
        if n_migrated:
            log.info("Boot: migrated %d entries to unified queue", n_migrated)
    except Exception as e:
        log.error("Unified queue migration failed: %s", e, exc_info=True)

    try:
        executed = await run_pending_migrations(plat, manager)
    except PersistenceUnavailableError:
        raise
    except Exception as e:
        log.error("Migration runner crashed: %s", e, exc_info=True)
        executed = []

    try:
        boot_text = "*Robyx v%s* — Headquarters online." % get_current_version()
        if executed:
            lines = ["- %s → %s" % (mid, status) for mid, status in executed]
            boot_text += "\n\n_Migrations applied:_\n" + "\n".join(lines)
        await plat.send_message(
            chat_id=control_room_id, text=boot_text, parse_mode="markdown",
        )
    except Exception as e:
        log.warning("Boot notification failed: %s", e)

    return executed


if __name__ == "__main__":
    # --smoke-test exits 0 right after all module-level imports have
    # completed. Used by bot/updater.py:_post_update_smoke_test() to
    # verify that a freshly-pulled release at least imports cleanly
    # before restarting the service. A successful pip install can still
    # leave the venv with a broken import graph (transitive dep conflict,
    # syntax error from a partial commit, missing migration constant);
    # running the same interpreter + code path as production catches
    # those before we return from apply_update with success.
    if "--smoke-test" in sys.argv:
        sys.exit(0)
    main()
