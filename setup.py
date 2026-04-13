#!/usr/bin/env python3
"""Robyx — Interactive setup wizard.

Configures the .env file with all required settings.
Auto-detects Chat ID and Owner ID when the bot is added to a Telegram group.
No external dependencies — stdlib only.

Non-interactive mode
--------------------
Pass all required parameters as CLI flags to skip interactive prompts.
This lets AI agents collect parameters first, then run setup in one shot.

  python3 setup.py --backend claude --platform telegram \
      --bot-token 123:ABC --chat-id -100123 --owner-id 456 \
      --workspace ~/Workspace --scheduler-interval 600

Platform-specific flags:
  Telegram: --bot-token, --chat-id, --owner-id
  Slack:    --slack-bot-token, --slack-app-token, --slack-channel-id, --slack-owner-id
  Discord:  --discord-bot-token, --discord-guild-id, --discord-channel-id, --discord-owner-id
            NOTE: Discord requires Developer Mode enabled to copy IDs
            (Discord Settings > Advanced > Developer Mode ON)

Optional: --openai-key, --scheduler-interval, --skip-test, --yes (overwrite .env)
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
ENV_FILE = PROJECT_ROOT / ".env"

# AI backends and their CLI binary names
BACKENDS = {
    "1": ("claude", "Claude Code CLI", "https://docs.anthropic.com/en/docs/claude-code"),
    "2": ("codex", "Codex CLI", "https://github.com/openai/codex"),
    "3": ("opencode", "OpenCode CLI", "https://github.com/opencode-ai/opencode"),
}

PLATFORMS = {
    "1": ("telegram", "Telegram"),
    "2": ("slack", "Slack"),
    "3": ("discord", "Discord"),
}

POLL_TIMEOUT = 30  # seconds per long-poll request


def banner():
    print()
    print("=" * 58)
    print("  Robyx Setup")
    print("  AI-powered agent staff")
    print("=" * 58)
    print()


def ask(prompt, default=None, required=True):
    """Ask the user for input."""
    suffix = " [%s]" % default if default else ""
    while True:
        value = input("%s%s: " % (prompt, suffix)).strip()
        if not value and default:
            return default
        if value or not required:
            return value
        print("  This field is required.")


def ask_choice(prompt, options):
    """Ask the user to choose from numbered options.

    Supports option tuples of length 2 (name, label) or 3 (name, label, url).
    """
    print(prompt)
    for key, vals in options.items():
        label = vals[1]
        print("  [%s] %s" % (key, label))
    while True:
        choice = input("Choice: ").strip()
        if choice in options:
            return options[choice]
        print("  Invalid choice. Try again.")


def validate_telegram_token(token):
    """Validate a Telegram bot token by calling getMe."""
    url = "https://api.telegram.org/bot%s/getMe" % token
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("ok"):
                bot_name = data["result"].get("username", "unknown")
                print("  Token valid! Bot: @%s" % bot_name)
                return True
            print("  Token rejected by Telegram.")
            return False
    except urllib.error.URLError as e:
        print("  Cannot reach Telegram API: %s" % e)
        return False
    except Exception as e:
        print("  Validation error: %s" % e)
        return False


def validate_chat_id(token, chat_id):
    """Validate that a chat ID belongs to a supergroup with topics enabled."""
    url = "https://api.telegram.org/bot%s/getChat" % token
    data = json.dumps({"chat_id": chat_id}).encode()
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                print("  Chat ID rejected by Telegram.")
                return False
            chat = result["result"]
            chat_type = chat.get("type", "unknown")
            title = chat.get("title", "unknown")
            has_topics = chat.get("is_forum", False)

            if chat_type != "supergroup":
                print("  '%s' is a %s, not a supergroup." % (title, chat_type))
                print("  Robyx requires a supergroup with topics enabled.")
                if chat_type == "group":
                    print("  Tip: convert it to a supergroup in group settings.")
                return False

            if not has_topics:
                print("  '%s' is a supergroup but topics are not enabled." % title)
                print("  Enable topics in: Group Settings > Topics.")
                return False

            print("  Valid! Supergroup: %s (topics enabled)" % title)
            return True
    except urllib.error.URLError as e:
        print("  Cannot reach Telegram API: %s" % e)
        return False
    except Exception as e:
        print("  Validation error: %s" % e)
        return False


def _telegram_api(token, method, payload=None):
    """Call a Telegram Bot API method. Returns the parsed JSON result or None."""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    try:
        if payload:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            req = urllib.request.Request(url, method="GET")

        timeout = (payload or {}).get("timeout", 0) + 10
        with urllib.request.urlopen(req, timeout=max(timeout, 10)) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return result.get("result")
    except Exception:
        pass
    return None


def _flush_updates(token):
    """Consume all pending updates and return the next offset."""
    updates = _telegram_api(token, "getUpdates", {"timeout": 0})
    if updates:
        return updates[-1]["update_id"] + 1
    return 0


def _check_update_for_group(update):
    """Check if an update contains a bot-added-to-supergroup event.

    Returns (chat_id, owner_id, group_title) or None.
    """
    member = update.get("my_chat_member")
    if not member:
        return None

    chat = member.get("chat", {})
    new_member = member.get("new_chat_member", {})
    from_user = member.get("from", {})

    # Only care about the bot being added (member/admin) to a supergroup with topics
    if new_member.get("status") not in ("member", "administrator"):
        return None
    if chat.get("type") != "supergroup":
        return None

    chat_id = chat.get("id")
    owner_id = from_user.get("id")
    title = chat.get("title", "Unknown")
    has_topics = chat.get("is_forum", False)

    return {
        "chat_id": chat_id,
        "owner_id": owner_id,
        "title": title,
        "has_topics": has_topics,
    }


def wait_for_group(token):
    """Wait for the bot to be added to a Telegram supergroup.

    Uses long polling on getUpdates. Returns (chat_id, owner_id, title)
    or None if the user cancels.
    """
    # Flush old updates so we only detect new additions
    offset = _flush_updates(token)

    print()
    print("  Now open Telegram and:")
    print("  1. Create a supergroup (or use an existing one)")
    print("  2. Enable Topics (Group Settings > Topics)")
    print("  3. Add the bot to the group")
    print()
    print("  Waiting for the bot to be added...")
    print("  (Press Ctrl+C to enter the Chat ID manually)")
    print()

    try:
        while True:
            updates = _telegram_api(token, "getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["my_chat_member"],
            })

            if not updates:
                continue

            for update in updates:
                offset = update["update_id"] + 1
                info = _check_update_for_group(update)
                if not info:
                    continue

                if not info["has_topics"]:
                    print("  Found group '%s' but topics are not enabled." % info["title"])
                    print("  Enable Topics in Group Settings, then remove and re-add the bot.")
                    print("  Still waiting...\n")
                    continue

                # Success!
                print("  Detected: '%s'" % info["title"])
                print("  Chat ID: %s" % info["chat_id"])
                print("  Owner ID: %s (you)" % info["owner_id"])
                return info["chat_id"], info["owner_id"], info["title"]

    except KeyboardInterrupt:
        print("\n")
        return None


def find_cli(name):
    """Find a CLI tool on PATH."""
    path = shutil.which(name)
    if path:
        print("  Found: %s" % path)
    return path


def send_test_message(token, chat_id):
    """Send a test message to the Telegram chat."""
    result = _telegram_api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "Robyx is configured and ready.\nTalk to me in the Main channel.",
        "parse_mode": "Markdown",
    })
    return result is not None


def _setup_telegram(config):
    """Configure Telegram-specific settings."""
    print("\n--- Telegram Bot Token ---\n")
    print("Get a token from @BotFather on Telegram.")
    while True:
        token = ask("Bot token")
        if validate_telegram_token(token):
            config["ROBYX_BOT_TOKEN"] = token
            break
        retry = ask("Try again? (Y/n)", default="y")
        if retry.lower() != "y":
            config["ROBYX_BOT_TOKEN"] = token
            print("  Saved unvalidated token.")
            break

    print("\n--- Connect to Telegram Group ---\n")

    detected = wait_for_group(config["ROBYX_BOT_TOKEN"])

    if detected:
        chat_id, owner_id, title = detected
        config["ROBYX_CHAT_ID"] = str(chat_id)
        config["ROBYX_OWNER_ID"] = str(owner_id)
    else:
        # Manual fallback
        print("  Manual configuration:")
        print()
        print("  Add the bot to your supergroup (with topics enabled),")
        print("  send a message, then check:")
        print("  https://api.telegram.org/bot%s/getUpdates" % config["ROBYX_BOT_TOKEN"])
        print()
        while True:
            chat_id = ask("Chat ID (negative number for groups)")
            if validate_chat_id(config["ROBYX_BOT_TOKEN"], chat_id):
                config["ROBYX_CHAT_ID"] = chat_id
                break
            retry = ask("Try again? (Y/n)", default="y")
            if retry.lower() != "y":
                config["ROBYX_CHAT_ID"] = chat_id
                print("  Saved unvalidated chat ID.")
                break

        print()
        print("  Send /start to @userinfobot on Telegram to find your user ID.")
        config["ROBYX_OWNER_ID"] = ask("Your Telegram user ID")


def _setup_slack(config):
    """Configure Slack-specific settings."""
    print("\n--- Slack Configuration ---\n")
    print("Create a Slack app at https://api.slack.com/apps")
    print("Required scopes: chat:write, channels:manage, channels:read, files:read")
    print("Enable Socket Mode and generate an App-Level Token (xapp-...).")
    print()

    config["SLACK_BOT_TOKEN"] = ask("Slack Bot Token (xoxb-...)")
    config["SLACK_APP_TOKEN"] = ask("Slack App-Level Token (xapp-...)")

    print()
    print("Enter the channel ID for the control room (#control-room).")
    print("You can find it in Slack: right-click channel > View channel details > scroll to bottom.")
    config["SLACK_CHANNEL_ID"] = ask("Control room channel ID (e.g. C01234ABCDE)")

    print()
    print("Enter your Slack user ID.")
    print("Find it: click your profile picture > Profile > ... > Copy member ID")
    config["SLACK_OWNER_ID"] = ask("Your Slack user ID (e.g. U01234ABCDE)")

    # Set placeholder values for Telegram-required config keys
    config["ROBYX_BOT_TOKEN"] = "unused-slack-mode"
    config["ROBYX_CHAT_ID"] = "0"
    config["ROBYX_OWNER_ID"] = "0"


def _discord_api(token, endpoint, method="GET", payload=None):
    """Call the Discord REST API. Returns parsed JSON or None."""
    url = "https://discord.com/api/v10%s" % endpoint
    try:
        if payload:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={
                    "Authorization": "Bot %s" % token,
                    "Content-Type": "application/json",
                },
                method=method,
            )
        else:
            req = urllib.request.Request(
                url,
                headers={"Authorization": "Bot %s" % token},
                method=method,
            )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _discord_validate_token(token):
    """Validate a Discord bot token. Returns (bot_id, bot_name) or None."""
    result = _discord_api(token, "/users/@me")
    if result and "id" in result:
        name = result.get("username", "unknown")
        print("  Token valid! Bot: %s (ID: %s)" % (name, result["id"]))
        return result["id"], name
    print("  Invalid token.")
    return None


def _discord_get_bot_id(token):
    """Extract bot client ID from token (base64 encoded in first segment)."""
    import base64
    try:
        return base64.b64decode(token.split(".")[0] + "==").decode()
    except Exception:
        return None


def _discord_wait_for_guild(token):
    """Poll Discord API waiting for the bot to be added to a server.

    Returns (guild_id, guild_name, owner_id) or None on Ctrl+C.
    """
    # Check if already in a guild
    guilds = _discord_api(token, "/users/@me/guilds")
    if guilds:
        g = guilds[0]
        guild_id = g["id"]
        # Get detailed guild info to find owner
        detail = _discord_api(token, "/guilds/%s" % guild_id)
        owner_id = detail.get("owner_id", "") if detail else ""
        print("  Detected: '%s' (ID: %s)" % (g["name"], guild_id))
        if owner_id:
            print("  Server owner ID: %s" % owner_id)
        return guild_id, g["name"], owner_id

    print()
    print("  Waiting for the bot to be added to a server...")
    print("  (Press Ctrl+C to enter IDs manually)")
    print()

    try:
        while True:
            time.sleep(3)
            guilds = _discord_api(token, "/users/@me/guilds")
            if guilds:
                g = guilds[0]
                guild_id = g["id"]
                detail = _discord_api(token, "/guilds/%s" % guild_id)
                owner_id = detail.get("owner_id", "") if detail else ""
                print("  Detected: '%s' (ID: %s)" % (g["name"], guild_id))
                if owner_id:
                    print("  Server owner: %s" % owner_id)
                return guild_id, g["name"], owner_id
    except KeyboardInterrupt:
        print("\n")
        return None


def _discord_find_or_create_channel(token, guild_id, name="control-room"):
    """Find or create a text channel in the guild. Returns channel_id or None."""
    # List existing channels
    channels = _discord_api(token, "/guilds/%s/channels" % guild_id)
    if channels:
        for ch in channels:
            if ch.get("name") == name and ch.get("type") == 0:  # type 0 = text
                print("  Found existing #%s (ID: %s)" % (name, ch["id"]))
                return ch["id"]

    # Create it
    result = _discord_api(token, "/guilds/%s/channels" % guild_id, method="POST", payload={
        "name": name,
        "type": 0,
        "topic": "Robyx Control Room — talk to Robyx here",
    })
    if result and "id" in result:
        print("  Created #%s (ID: %s)" % (name, result["id"]))
        return result["id"]

    print("  Could not create #%s channel." % name)
    return None


def _setup_discord(config):
    """Configure Discord-specific settings."""

    # Step 2: Bot token
    print("\n--- Step 2: Create a Discord Bot ---\n")
    print("  1. Open https://discord.com/developers/applications")
    print("  2. Click 'New Application' → name it (e.g. 'Robyx')")
    print("  3. Left menu → 'Bot'")
    print("  4. Click 'Reset Token' → copy the token")
    print("  5. Scroll down → enable 'MESSAGE CONTENT INTENT'")
    print()

    while True:
        token = ask("Paste the bot token here")
        info = _discord_validate_token(token)
        if info:
            bot_id, bot_name = info
            config["DISCORD_BOT_TOKEN"] = token
            break
        retry = ask("Try again? (Y/n)", default="y")
        if retry.lower() != "y":
            config["DISCORD_BOT_TOKEN"] = token
            bot_id = _discord_get_bot_id(token) or "UNKNOWN"
            break

    # Step 3: Add bot to server (auto-detect)
    # Permissions: Send Messages (2048) + Manage Channels (16) + Read History (65536)
    #   + Create Public Threads (34359738368) + Send in Threads (274877906944)
    permissions = 2048 | 16 | 65536 | 34359738368 | 274877906944
    invite_url = "https://discord.com/oauth2/authorize?client_id=%s&scope=bot&permissions=%d" % (
        bot_id, permissions,
    )

    print("\n--- Step 3: Add the Bot to a Server ---\n")
    print("  Open this link in your browser:")
    print()
    print("  %s" % invite_url)
    print()
    print("  Select a server (or create a new one) and click 'Authorize'.")
    print()

    detected = _discord_wait_for_guild(config["DISCORD_BOT_TOKEN"])

    if detected:
        guild_id, guild_name, owner_id = detected
        config["DISCORD_GUILD_ID"] = guild_id
        config["DISCORD_OWNER_ID"] = owner_id or ""

        # Auto-create or find #control-room
        print()
        channel_id = _discord_find_or_create_channel(config["DISCORD_BOT_TOKEN"], guild_id)
        config["DISCORD_CONTROL_CHANNEL_ID"] = channel_id or "0"

        if not owner_id:
            print()
            print("  Could not detect your user ID automatically.")
            print("  Enable Developer Mode (Settings > Advanced), then")
            print("  right-click your username > Copy User ID")
            config["DISCORD_OWNER_ID"] = ask("Your Discord user ID")
    else:
        # Manual fallback
        print("  Manual configuration:")
        print()
        print("  First, enable Developer Mode in Discord:")
        print("    Settings (gear icon) > App Settings > Advanced > Developer Mode = ON")
        print("  This makes 'Copy ID' options visible when you right-click items.")
        print()
        config["DISCORD_GUILD_ID"] = ask("Server ID (right-click the server name > Copy Server ID)")
        config["DISCORD_CONTROL_CHANNEL_ID"] = ask("Control channel ID (or Enter to auto-create)", required=False) or "0"
        print()
        config["DISCORD_OWNER_ID"] = ask("Your user ID (right-click your username > Copy User ID)")

    # Set placeholder values for Telegram-required config keys
    config["ROBYX_BOT_TOKEN"] = "unused-discord-mode"
    config["ROBYX_CHAT_ID"] = "0"
    config["ROBYX_OWNER_ID"] = config["DISCORD_OWNER_ID"]


def setup():
    banner()

    if ENV_FILE.exists():
        overwrite = ask("An .env file already exists. Overwrite? (y/N)", default="n")
        if overwrite.lower() != "y":
            print("Setup cancelled.")
            return

    config = {}

    # Step 1: AI Backend
    print("\n--- Step 1: AI Backend ---\n")
    backend_name, backend_label, backend_url = ask_choice(
        "Which AI CLI tool do you use?", BACKENDS
    )
    config["AI_BACKEND"] = backend_name

    cli_path = find_cli(backend_name)
    if not cli_path:
        print("  '%s' not found on PATH." % backend_name)
        print("  Install it from: %s" % backend_url)
        custom = ask("Enter full path to the CLI binary (or press Enter to skip)", required=False)
        if custom and os.path.isfile(custom):
            cli_path = custom
        else:
            print("  Warning: CLI not found. You'll need to set AI_CLI_PATH in .env manually.")
    config["AI_CLI_PATH"] = cli_path or ""

    # Step 2: Messaging Platform
    print("\n--- Step 2: Messaging Platform ---\n")
    platform_choice = ask_choice("Which messaging platform?", PLATFORMS)
    platform_name = platform_choice[0]
    config["ROBYX_PLATFORM"] = platform_name

    if platform_name == "slack":
        _setup_slack(config)
    elif platform_name == "discord":
        _setup_discord(config)
    else:
        _setup_telegram(config)

    # Step 4: Workspace
    print("\n--- Step 4: Workspace Directory ---\n")
    default_ws = os.path.expanduser("~/Workspace")
    config["ROBYX_WORKSPACE"] = ask("Workspace root directory", default=default_ws)

    # Step 5: Voice (optional)
    print("\n--- Step 5: Voice Transcription (optional) ---\n")
    print("Voice messages are transcribed using OpenAI Whisper.")
    print("You can also add the API key later by telling Robyx in chat.")
    print()
    voice = ask("Enter OpenAI API key (or press Enter to skip)", required=False)
    config["OPENAI_API_KEY"] = voice or ""

    # Step 6: Scheduler interval
    print("\n--- Step 6: Scheduler ---\n")
    config["SCHEDULER_INTERVAL"] = ask("Unified scheduler tick interval in seconds", default="60")
    config["UPDATE_CHECK_INTERVAL"] = "3600"
    config["CLAUDE_PERMISSION_MODE"] = ""

    # Write .env
    print("\n--- Writing configuration ---\n")
    lines = []
    for key, value in config.items():
        lines.append('%s=%s' % (key, value))

    ENV_FILE.write_text("\n".join(lines) + "\n")
    print("  Configuration written to %s" % ENV_FILE)

    # Create data directory
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    print("  Data directory created.")

    # Send test message (Telegram only — Slack requires async runtime)
    if platform_name == "telegram":
        print("\n--- Test Message ---\n")
        send_test = ask("Send a test message to your Telegram chat? (Y/n)", default="y")
        if send_test.lower() == "y":
            if send_test_message(config["ROBYX_BOT_TOKEN"], config["ROBYX_CHAT_ID"]):
                print("  Test message sent successfully!")
            else:
                print("  Could not send test message. Check your chat ID.")

    # Summary
    print("\n" + "=" * 58)
    print("  Setup complete!")
    print("=" * 58)
    print()
    print("Next steps:")
    system = platform.system()
    if system == "Darwin":
        print("  ./install/install-mac.sh")
    elif system == "Linux":
        print("  ./install/install-linux.sh")
    elif system == "Windows":
        print("  powershell install/install-windows.ps1")
    else:
        print("  python3 bot/bot.py  (manual start)")
    print()


def parse_args():
    """Parse CLI arguments for non-interactive setup."""
    p = argparse.ArgumentParser(
        description="Robyx setup — interactive or non-interactive",
        epilog="When all required flags are provided, setup runs without prompts.",
    )

    p.add_argument("--backend", choices=["claude", "codex", "opencode"],
                    help="AI backend CLI tool")
    p.add_argument("--platform", choices=["telegram", "slack", "discord"],
                    help="Messaging platform")

    # Telegram
    p.add_argument("--bot-token",
                    help="Telegram bot token (from @BotFather → /newbot)")
    p.add_argument("--chat-id",
                    help="Telegram chat ID (negative number, e.g. -100123456789; "
                         "find via https://api.telegram.org/bot<TOKEN>/getUpdates "
                         "after adding bot to a supergroup with Topics enabled)")
    p.add_argument("--owner-id",
                    help="Telegram owner user ID (message @userinfobot to get yours)")

    # Slack
    p.add_argument("--slack-bot-token",
                    help="Slack Bot Token (xoxb-...; from api.slack.com/apps → OAuth)")
    p.add_argument("--slack-app-token",
                    help="Slack App-Level Token (xapp-...; Basic Information → App-Level "
                         "Tokens → generate with connections:write scope; also enable Socket Mode)")
    p.add_argument("--slack-channel-id",
                    help="Slack control channel ID (right-click channel → View details → "
                         "scroll to bottom, e.g. C01234ABCDE)")
    p.add_argument("--slack-owner-id",
                    help="Slack user ID (click profile → Profile → ⋯ → Copy member ID, "
                         "e.g. U01234ABCDE)")

    # Discord — IMPORTANT: Developer Mode must be enabled in Discord
    # (Settings → Advanced → Developer Mode) to see "Copy ID" options.
    p.add_argument("--discord-bot-token",
                    help="Discord bot token (discord.com/developers/applications → your app "
                         "→ Bot → Reset Token; also enable Message Content Intent)")
    p.add_argument("--discord-guild-id",
                    help="Discord server ID (enable Developer Mode first: Discord Settings → "
                         "Advanced → Developer Mode ON; then right-click server → Copy Server ID)")
    p.add_argument("--discord-channel-id",
                    help="Discord control channel ID (pass explicitly for non-interactive "
                         "setup; the interactive wizard usually creates or discovers "
                         "#control-room for you; right-click channel → Copy Channel ID)")
    p.add_argument("--discord-owner-id",
                    help="Discord user ID (right-click your username → Copy User ID; "
                         "requires Developer Mode enabled)")

    # Common
    p.add_argument("--workspace", help="Workspace root directory")
    p.add_argument("--openai-key", default="", help="OpenAI API key for voice transcription")
    p.add_argument("--scheduler-interval", default="600", help="Scheduler interval in seconds")
    p.add_argument("--skip-test", action="store_true", help="Skip sending test message")
    p.add_argument("--yes", "-y", action="store_true", help="Overwrite existing .env without asking")

    return p.parse_args()


def _backend_info(name):
    """Return (cli_name, label, url) for a backend name."""
    for vals in BACKENDS.values():
        if vals[0] == name:
            return vals
    return None


def setup_noninteractive(args):
    """Run setup without interactive prompts using CLI arguments."""
    banner()

    if ENV_FILE.exists() and not args.yes:
        print("Error: .env already exists. Use --yes to overwrite.")
        sys.exit(1)

    config = {}

    # Backend
    info = _backend_info(args.backend)
    config["AI_BACKEND"] = info[0]
    cli_path = find_cli(info[0])
    if not cli_path:
        print("  Warning: '%s' not found on PATH. Set AI_CLI_PATH in .env manually." % info[0])
    config["AI_CLI_PATH"] = cli_path or ""

    # Platform
    config["ROBYX_PLATFORM"] = args.platform

    if args.platform == "telegram":
        config["ROBYX_BOT_TOKEN"] = args.bot_token
        config["ROBYX_CHAT_ID"] = args.chat_id
        config["ROBYX_OWNER_ID"] = args.owner_id

        # Validate token
        print("\n--- Validating Telegram token ---")
        if not validate_telegram_token(args.bot_token):
            print("  Warning: token validation failed, continuing anyway.")

        # Validate chat ID
        print("\n--- Validating Chat ID ---")
        if not validate_chat_id(args.bot_token, args.chat_id):
            print("  Warning: chat ID validation failed, continuing anyway.")

    elif args.platform == "slack":
        config["SLACK_BOT_TOKEN"] = args.slack_bot_token
        config["SLACK_APP_TOKEN"] = args.slack_app_token
        config["SLACK_CHANNEL_ID"] = args.slack_channel_id
        config["SLACK_OWNER_ID"] = args.slack_owner_id
        config["ROBYX_BOT_TOKEN"] = "unused-slack-mode"
        config["ROBYX_CHAT_ID"] = "0"
        config["ROBYX_OWNER_ID"] = "0"

    elif args.platform == "discord":
        config["DISCORD_BOT_TOKEN"] = args.discord_bot_token
        config["DISCORD_GUILD_ID"] = args.discord_guild_id
        config["DISCORD_CONTROL_CHANNEL_ID"] = args.discord_channel_id or "0"
        config["DISCORD_OWNER_ID"] = args.discord_owner_id
        config["ROBYX_BOT_TOKEN"] = "unused-discord-mode"
        config["ROBYX_CHAT_ID"] = "0"
        config["ROBYX_OWNER_ID"] = args.discord_owner_id

        # Validate token
        print("\n--- Validating Discord token ---")
        if not _discord_validate_token(args.discord_bot_token):
            print("  Warning: token validation failed, continuing anyway.")

    # Workspace
    config["ROBYX_WORKSPACE"] = args.workspace or os.path.expanduser("~/Workspace")

    # Optional
    config["OPENAI_API_KEY"] = args.openai_key
    config["SCHEDULER_INTERVAL"] = args.scheduler_interval
    config["UPDATE_CHECK_INTERVAL"] = "3600"
    config["CLAUDE_PERMISSION_MODE"] = ""

    # Write .env
    print("\n--- Writing configuration ---\n")
    lines = ['%s=%s' % (k, v) for k, v in config.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    print("  Configuration written to %s" % ENV_FILE)

    # Create data directory
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    print("  Data directory created.")

    # Test message (Telegram only)
    if args.platform == "telegram" and not args.skip_test:
        print("\n--- Test Message ---\n")
        if send_test_message(config["ROBYX_BOT_TOKEN"], config["ROBYX_CHAT_ID"]):
            print("  Test message sent successfully!")
        else:
            print("  Could not send test message. Check your chat ID.")

    # Summary
    print("\n" + "=" * 58)
    print("  Setup complete!")
    print("=" * 58)
    print()
    print("Next steps:")
    system = platform.system()
    if system == "Darwin":
        print("  ./install/install-mac.sh")
    elif system == "Linux":
        print("  ./install/install-linux.sh")
    elif system == "Windows":
        print("  powershell install/install-windows.ps1")
    else:
        print("  python3 bot/bot.py  (manual start)")
    print()


def _has_required_args(args):
    """Check if enough CLI args were provided for non-interactive mode."""
    if not args.backend or not args.platform:
        return False
    if args.platform == "telegram":
        return all([args.bot_token, args.chat_id, args.owner_id])
    if args.platform == "slack":
        return all([args.slack_bot_token, args.slack_app_token,
                    args.slack_channel_id, args.slack_owner_id])
    if args.platform == "discord":
        return all([args.discord_bot_token, args.discord_guild_id,
                    args.discord_channel_id, args.discord_owner_id])
    return False


def _looks_like_noninteractive_request(args):
    """Return True if the user supplied any setup flag at all."""
    return any([
        args.backend,
        args.platform,
        args.bot_token,
        args.chat_id,
        args.owner_id,
        args.slack_bot_token,
        args.slack_app_token,
        args.slack_channel_id,
        args.slack_owner_id,
        args.discord_bot_token,
        args.discord_guild_id,
        args.discord_channel_id,
        args.discord_owner_id,
        args.workspace,
        args.openai_key,
        args.scheduler_interval != "600",
        args.skip_test,
        args.yes,
    ])


def _missing_required_args(args):
    """Return the missing flags for a requested non-interactive run."""
    missing = []
    if not args.backend:
        missing.append("--backend")
    if not args.platform:
        missing.append("--platform")

    if args.platform == "telegram":
        if not args.bot_token:
            missing.append("--bot-token")
        if not args.chat_id:
            missing.append("--chat-id")
        if not args.owner_id:
            missing.append("--owner-id")
    elif args.platform == "slack":
        if not args.slack_bot_token:
            missing.append("--slack-bot-token")
        if not args.slack_app_token:
            missing.append("--slack-app-token")
        if not args.slack_channel_id:
            missing.append("--slack-channel-id")
        if not args.slack_owner_id:
            missing.append("--slack-owner-id")
    elif args.platform == "discord":
        if not args.discord_bot_token:
            missing.append("--discord-bot-token")
        if not args.discord_guild_id:
            missing.append("--discord-guild-id")
        if not args.discord_channel_id:
            missing.append("--discord-channel-id")
        if not args.discord_owner_id:
            missing.append("--discord-owner-id")

    return missing


if __name__ == "__main__":
    args = parse_args()
    if _has_required_args(args):
        setup_noninteractive(args)
    elif _looks_like_noninteractive_request(args):
        print(
            "Error: missing required flags for non-interactive setup: %s"
            % ", ".join(_missing_required_args(args))
        )
        sys.exit(2)
    else:
        setup()
