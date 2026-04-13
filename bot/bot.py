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
# bot/requirements.txt so a new release with new deps never boots against
# a stale environment. See bot/_bootstrap.py for the full rationale.
import _bootstrap
_bootstrap.ensure_dependencies()
# v0.16+: relocate any leftover repo-root runtime files into data/. This
# is the boot-time safety net that complements the pre-pull migration in
# bot/updater.py — see migrate_personal_data_if_needed() for rationale.
_bootstrap.migrate_personal_data_if_needed()

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
    TIMED_SCHEDULER_INTERVAL,
    UPDATE_CHECK_INTERVAL,
)
from handlers import make_handlers
from messaging.base import PlatformMessage
from migrations import run_pending as run_pending_migrations
from scheduler import run_scheduler_cycle
from timed_scheduler import migrate_oneshot_from_tasks_md, run_timed_cycle
from topics import heal_detached_workspaces
from updater import apply_update, check_for_updates, get_pending_update, restart_service
from voice import is_available as voice_available

log = logging.getLogger("robyx")

PID_FILE = DATA_DIR / "bot.pid"
REMINDER_INTERVAL = 60
REMINDER_BOOT_DELAY = 5

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
    """Verify no other bot instance is running. Write PID file for current process."""
    from process import is_pid_alive, is_bot_process, get_process_name

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_alive(pid):
                if is_bot_process(pid):
                    sys.exit("Robyx already running (PID %d)" % pid)
                proc_name = get_process_name(pid)
                log.warning("Stale PID file: PID %d is now '%s'. Overwriting.", pid, proc_name)
            else:
                log.warning("Stale PID file found. Overwriting.")
        except ValueError:
            log.warning("Corrupt PID file found. Overwriting.")

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    """Job: run one scheduler cycle."""
    backend = context.job.data["backend"]
    plat = context.job.data["platform"]
    control_room_id = plat.control_room_id
    log.info("Scheduler: running cycle")

    try:
        result = await run_scheduler_cycle(backend, platform=plat)

        # Notify in Main only if something happened
        if result["dispatched"] or result["errors"]:
            lines = []
            for name, pid in result["dispatched"]:
                lines.append("Dispatched: %s (PID %d)" % (name, pid))
            for name in result["errors"]:
                lines.append("Error: %s" % name)

            text = "*Scheduler*\n" + "\n".join(lines)
            try:
                await plat.send_message(
                    chat_id=CHAT_ID,
                    text=text,
                    thread_id=control_room_id,
                    parse_mode="markdown",
                )
            except Exception as e:
                log.error("Failed to send scheduler notification: %s", e)

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
        info = check_for_updates()
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


def reminders_file_path():
    """Return the runtime reminders file path."""
    return DATA_DIR / "reminders.json"


async def run_reminder_cycle(platform, default_chat_id=None, reminders_file=None):
    """Run one reminder-engine pass for the given platform."""
    from reminders import check_reminders

    await check_reminders(
        reminders_file=reminders_file or reminders_file_path(),
        platform=platform,
        default_chat_id=default_chat_id,
    )


async def reminder_job(context):
    """Telegram job-queue wrapper for one reminder-engine pass."""
    await run_reminder_cycle(
        context.job.data["platform"],
        default_chat_id=context.job.data.get("default_chat_id"),
        reminders_file=context.job.data.get("reminders_file"),
    )


async def _slack_reminder_loop(plat):
    """Background loop: run reminder cycles on Slack."""
    await asyncio.sleep(REMINDER_BOOT_DELAY)
    while True:
        try:
            await run_reminder_cycle(
                plat,
                default_chat_id=getattr(plat, "control_room_channel", None),
            )
        except Exception as e:
            log.error("Reminder engine cycle failed: %s", e, exc_info=True)
        await asyncio.sleep(REMINDER_INTERVAL)


async def _discord_reminder_loop(plat, client):
    """Background loop: run reminder cycles on Discord."""
    await client.wait_until_ready()
    await asyncio.sleep(REMINDER_BOOT_DELAY)
    while not client.is_closed():
        try:
            await run_reminder_cycle(
                plat,
                default_chat_id=plat.control_room_id,
            )
        except Exception as e:
            log.error("Reminder engine cycle failed: %s", e, exc_info=True)
        await asyncio.sleep(REMINDER_INTERVAL)


def main():
    setup_logging()
    ensure_single_instance()
    log.info("Starting Robyx...")

    # Initialize AI backend
    backend = create_backend(AI_BACKEND, AI_CLI_PATH or None)
    log.info("AI backend: %s (%s)", backend.name, backend.cli_path)

    # Initialize agent manager
    manager = AgentManager()

    # Create platform
    if PLATFORM == "telegram":
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

    def save_on_exit(*_args):
        log.info("Shutting down — saving state...")
        manager.save_state()
        PID_FILE.unlink(missing_ok=True)

    atexit.register(save_on_exit)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *a: (save_on_exit(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *a: (save_on_exit(), sys.exit(0)))

    # Build handlers
    h = make_handlers(manager, backend)

    if PLATFORM == "slack":
        _run_slack(plat, h, backend, manager)
    elif PLATFORM == "discord":
        _run_discord(plat, h, backend, manager)
    else:
        _run_telegram(plat, h, backend, manager)


def _run_telegram(plat, h, backend, manager):
    """Start the Telegram event loop."""
    # Build Telegram application
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

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
    for name in ("start", "help", "workspaces", "specialists", "status", "reset", "focus", "ping", "checkupdate", "doupdate"):
        app.add_handler(CommandHandler(name, _wrap_command(h[name])))

    # Register voice handler (always — replies gracefully if Whisper not configured)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _wrap_voice(h["voice"])))
    log.info("Voice transcription: %s", "enabled" if voice_available() else "disabled (no OPENAI_API_KEY)")

    # Register text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _wrap_message(h["message"])))

    # Scheduler: runs every SCHEDULER_INTERVAL seconds
    log.info("Scheduler: interval=%ds", SCHEDULER_INTERVAL)
    app.job_queue.run_repeating(
        scheduler_job,
        interval=SCHEDULER_INTERVAL,
        first=10,  # First run 10 seconds after startup
        data={"backend": backend, "platform": plat},
    )

    # Timed scheduler: runs every TIMED_SCHEDULER_INTERVAL seconds (default 60s)
    async def timed_scheduler_job(context):
        try:
            await run_timed_cycle(
                context.job.data["backend"],
                platform=context.job.data["platform"],
            )
        except Exception as e:
            log.error("Timed scheduler cycle failed: %s", e, exc_info=True)

    log.info("Timed scheduler: interval=%ds", TIMED_SCHEDULER_INTERVAL)
    app.job_queue.run_repeating(
        timed_scheduler_job,
        interval=TIMED_SCHEDULER_INTERVAL,
        first=5,  # First run 5 seconds after startup (after migration)
        data={"backend": backend, "platform": plat},
    )

    # Update checker: runs every UPDATE_CHECK_INTERVAL seconds.
    # The manager is included in the job data so apply_update can route
    # session invalidation through manager.reset_sessions and avoid the
    # state.json clobber that bit v0.15.0 / v0.15.1.
    log.info("Update checker: interval=%ds", UPDATE_CHECK_INTERVAL)
    app.job_queue.run_repeating(
        update_check_job,
        interval=UPDATE_CHECK_INTERVAL,
        first=30,  # First check 30 seconds after startup
        data={"platform": plat, "manager": manager},
    )

    # Reminder engine: runs every 60 seconds, fires due reminders directly (no LLM)
    app.job_queue.run_repeating(
        reminder_job,
        interval=REMINDER_INTERVAL,
        first=REMINDER_BOOT_DELAY,  # First check shortly after startup
        data={"platform": plat, "default_chat_id": CHAT_ID},
    )
    log.info(
        "Reminder engine: interval=%ds, file=%s",
        REMINDER_INTERVAL,
        reminders_file_path(),
    )

    # Send boot notification
    async def boot_notify(context):
        from updater import get_current_version
        boot_plat = context.job.data["platform"]
        boot_manager = context.job.data["manager"]
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

        # Migrate any legacy one-shot tasks from tasks.md → timed_queue.json
        try:
            n_migrated = migrate_oneshot_from_tasks_md()
            if n_migrated:
                log.info("Boot: migrated %d one-shot task(s) to timed queue", n_migrated)
        except Exception as e:
            log.error("One-shot migration failed: %s", e, exc_info=True)

        # Run pending migrations (rename channels, state patches, etc.)
        # BEFORE the boot message goes out — that way the boot message lands
        # in the correctly-named channel and migrations that fail are
        # visible in the boot summary. Pass the manager so state-mutating
        # migrations can use ``manager.reset_sessions(...)`` instead of
        # touching ``state.json`` directly (which would be clobbered by
        # the running bot's next ``save_state()`` call).
        try:
            executed = await run_pending_migrations(boot_plat, boot_manager)
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

    app.job_queue.run_once(boot_notify, when=3, data={"platform": plat, "manager": manager})

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
                "reset", "focus", "ping", "checkupdate", "doupdate")

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

    async def _run():
        control_room = plat.control_room_channel
        await _run_boot_sequence(plat, manager, control_room)

        # Start background loops
        asyncio.ensure_future(_background_scheduler_loop(plat, backend, control_room))
        asyncio.ensure_future(_background_timed_scheduler_loop(plat, backend))
        asyncio.ensure_future(_background_update_loop(plat, control_room, manager))
        asyncio.ensure_future(_slack_reminder_loop(plat))
        log.info(
            "Reminder engine: interval=%ds, file=%s",
            REMINDER_INTERVAL,
            reminders_file_path(),
        )

        handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
        log.info("Robyx is running on Slack (Socket Mode)...")
        await handler.start_async()

    asyncio.run(_run())


def _run_discord(plat, h, backend, manager):
    """Start the Discord event loop using discord.py."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    client = discord.Client(intents=intents)
    plat.set_bot(client)

    COMMANDS = ("start", "help", "workspaces", "specialists", "status",
                "reset", "focus", "ping", "checkupdate", "doupdate")

    @client.event
    async def on_ready():
        log.info("Discord bot ready: %s", client.user)
        control_room = plat.control_room_id
        await _run_boot_sequence(plat, manager, control_room)

        # Start background loops
        client.loop.create_task(_background_scheduler_loop(plat, backend, control_room))
        client.loop.create_task(_background_timed_scheduler_loop(plat, backend))
        client.loop.create_task(_background_update_loop(plat, control_room, manager))
        client.loop.create_task(_discord_reminder_loop(plat, client))
        log.info(
            "Reminder engine: interval=%ds, file=%s",
            REMINDER_INTERVAL,
            reminders_file_path(),
        )

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if not plat.is_owner(message.author.id):
            return

        # Check for voice message
        audio_attachment = None
        for a in message.attachments:
            if a.content_type and "audio" in a.content_type:
                audio_attachment = a
                break

        if message.flags.voice or audio_attachment:
            msg = PlatformMessage(
                user_id=message.author.id,
                chat_id=message.guild.id if message.guild else message.channel.id,
                text=None,
                thread_id=message.channel.id,
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
                    chat_id=message.guild.id if message.guild else message.channel.id,
                    text=text,
                    thread_id=message.channel.id,
                    command=cmd_name,
                    args=args,
                )
                await h[cmd_name](plat, msg, message)
                return

        # Regular text message
        msg = PlatformMessage(
            user_id=message.author.id,
            chat_id=message.guild.id if message.guild else message.channel.id,
            text=text,
            thread_id=message.channel.id,
        )
        await h["message"](plat, msg, message)

    client.run(DISCORD_BOT_TOKEN)


# ── Shared background loops ───────────────────────────────────────────────
#
# These are used by Slack and Discord runners (Telegram uses PTB's
# job_queue instead). Each loop runs a single concern on a fixed interval,
# catching exceptions so one failure never crashes the event loop.


async def _background_scheduler_loop(
    plat, backend, control_room_id, *, interval: int = SCHEDULER_INTERVAL,
) -> None:
    """Periodically run the main scheduler cycle and notify on activity."""
    while True:
        await asyncio.sleep(interval)
        log.info("Scheduler: running cycle")
        try:
            result = await run_scheduler_cycle(backend, platform=plat)
            if result["dispatched"] or result["errors"]:
                lines = []
                for name, pid in result["dispatched"]:
                    lines.append("Dispatched: %s (PID %d)" % (name, pid))
                for name in result["errors"]:
                    lines.append("Error: %s" % name)
                text = "*Scheduler*\n" + "\n".join(lines)
                try:
                    await plat.send_message(
                        chat_id=control_room_id, text=text, parse_mode="markdown",
                    )
                except Exception as e:
                    log.error("Failed to send scheduler notification: %s", e)
        except Exception as e:
            log.error("Scheduler cycle failed: %s", e, exc_info=True)


async def _background_timed_scheduler_loop(
    plat, backend, *, interval: int = TIMED_SCHEDULER_INTERVAL,
) -> None:
    """Periodically run the timed-scheduler cycle."""
    await asyncio.sleep(5)  # Let boot finish first
    while True:
        try:
            await run_timed_cycle(backend, platform=plat)
        except Exception as e:
            log.error("Timed scheduler cycle failed: %s", e, exc_info=True)
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
            info = check_for_updates()
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
        n_migrated = migrate_oneshot_from_tasks_md()
        if n_migrated:
            log.info("Boot: migrated %d one-shot task(s) to timed queue", n_migrated)
    except Exception as e:
        log.error("One-shot migration failed: %s", e, exc_info=True)

    try:
        executed = await run_pending_migrations(plat, manager)
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
    main()
