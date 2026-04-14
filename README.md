<div align="center">

<img src="robyx-banner.png" alt="Robyx" width="100%">

**Your AI staff, managed from chat.**

Clone. Configure. Talk. You get a team of AI agents — each with its own chat topic/channel — orchestrated by **Robyx**, your principal coordinator. Works on **Telegram**, **Discord**, and **Slack**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)]()
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4.svg?logo=telegram&logoColor=white)](https://core.telegram.org/bots)

</div>

---

## Why Robyx

There are many AI agent platforms out there — some with hundreds of pre-built skills, integrations with every service imaginable, and complex configuration systems.

Robyx takes the opposite approach.

**You don't get a pre-built team. You build your own.**

Robyx gives you a blank canvas: an orchestrator, a control room on your messaging platform, and the ability to create agents through conversation. You tell Robyx what you need, and it creates the workspace, the agent, the instructions — all on the fly. Need a deployment specialist? Ask Robyx. Need a scheduled monitor? Describe what it should check. Need a code reviewer that knows your architecture? Define it in natural language.

No marketplace. No pre-packaged skills. No vendor lock-in. Just a simple system that grows with your needs — from one agent to a full team, built bottom-up from your actual requirements.

**The philosophy is simple**: one control room, one AI CLI tool, infinite flexibility.

---

## Documentation

| Topic | What's inside |
|-------|---------------|
| [Architecture](docs/architecture.md) | How it works, the three roles (Robyx / workspaces / specialists), workspace lifecycle |
| [Scheduler](docs/scheduler.md) | Reminders, one-shot, periodic, and continuous tasks; agent interruption; runtime contract |
| [Memory System](docs/memory.md) | Active + archive memory tiers, integration with existing Claude Code memory |
| [Building Your Team](docs/team.md) | How to grow your fleet of agents through conversation |
| [Configuration](docs/configuration.md) | Every env var, per-platform settings, in-chat config updates, platform migration |
| [Commands](docs/commands.md) | Slash command reference (Telegram + Discord) |
| [AI Backends](docs/backends.md) | Claude Code / Codex / OpenCode, autonomous-by-default permissions, `models.yaml` |
| [Voice + Images](docs/media.md) | Voice transcription via Whisper, agent-initiated image delivery |
| [Auto-Updates + Migrations + Service Management](docs/updates.md) | Update flow with snapshots & smoke tests, migration framework, service installers |

Two more useful refs at the repo root:
- [`ORCHESTRATOR.md`](ORCHESTRATOR.md) — Robyx's behaviour reference (`[REMIND]`, `[DELEGATE]`, etc.)
- [`AGENTS.md`](AGENTS.md) — Conventions for agent / specialist briefs
- [`CHANGELOG.md`](CHANGELOG.md) — Per-version changes; full notes under [`releases/`](releases/)

---

## Quick Start

### Prerequisites

- Python 3.10+
- One CLI-based AI tool: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | [Codex CLI](https://github.com/openai/codex) | [OpenCode](https://github.com/opencode-ai/opencode)
- A messaging platform: **Telegram**, **Discord**, or **Slack**

The setup wizard guides you through everything. You can switch platforms at any time by telling Robyx — all your workspaces, agents, and memory are preserved.

### Step 1: Clone and run the setup wizard

```bash
git clone https://github.com/terrordrummer/robyx-ai.git && cd robyx-ai
python3 setup.py
```

The wizard asks which platform to use, then walks you through the setup.

<!-- AI-AGENT INSTRUCTIONS (not rendered in GitHub UI)

If you are an AI agent installing Robyx on behalf of a user, do NOT suggest
running `python3 setup.py` interactively. Instead, collect the required
parameters by guiding the user step-by-step, then run setup non-interactively:

  python3 setup.py --backend <backend> --platform <platform> [platform flags] --workspace <path> -y

The setup validates tokens and IDs against platform APIs automatically.
Run `python3 setup.py --help` for the full flag reference with "how to obtain" instructions.

TELEGRAM — required flags:
  --bot-token    Telegram bot token (from @BotFather → /newbot)
  --chat-id      Chat ID (negative number; add bot to supergroup with Topics, then check
                 https://api.telegram.org/bot<TOKEN>/getUpdates for "chat":{"id":-100...})
  --owner-id     User ID (message @userinfobot on Telegram)

DISCORD — required flags:
  PREREQUISITE: user must enable Developer Mode in Discord first
  (Settings → App Settings → Advanced → Developer Mode ON) to see "Copy ID" options.

  --discord-bot-token   Bot token (discord.com/developers/applications → app → Bot → Reset Token;
                        also enable Message Content Intent on the same page)
  --discord-guild-id    Server ID (right-click server name → Copy Server ID)
  --discord-owner-id    User ID (right-click own username → Copy User ID)
  --discord-channel-id  Control-room channel ID (pass it explicitly in non-interactive setup;
                        the interactive wizard usually creates or discovers #control-room for you)

  BOT PERMISSIONS: when creating the bot at discord.com/developers/applications → OAuth2 → URL Generator,
  select scope "bot" and enable these permissions:
    - Send Messages
    - Manage Channels
    - Read Message History
    - Create Public Threads
    - Send Messages in Threads

SLACK — required flags:
  --slack-bot-token   Bot Token (xoxb-...; api.slack.com/apps → app → OAuth & Permissions)
  --slack-app-token   App-Level Token (xapp-...; Basic Information → App-Level Tokens →
                      generate with connections:write scope; also enable Socket Mode)
  --slack-channel-id  Channel ID (right-click channel → View details → scroll to bottom)
  --slack-owner-id    User ID (click profile → Profile → ⋯ → Copy member ID)
  Required OAuth scopes: chat:write, channels:manage, channels:read, files:read

COMMON optional flags:
  --openai-key KEY           OpenAI API key for voice transcription
  --scheduler-interval SEC   Scheduler check interval (default: 600)
  --skip-test                Skip sending test message (Telegram only)
  --yes / -y                 Overwrite existing .env without asking

Example (Discord):
  python3 setup.py --backend claude --platform discord \
      --discord-bot-token "TOKEN" --discord-guild-id "123" \
      --discord-channel-id "789" --discord-owner-id "456" -y

UNINSTALLING ROBYX
====================
If the user asks to uninstall, stop, or remove Robyx, you MUST follow the correct
order below. The service has keep-alive/auto-restart enabled — if you just kill the
process it will be restarted immediately by the OS.

macOS (launchd):
  The service has KeepAlive=true. You must unload the plist FIRST (this both stops the
  process and removes the keep-alive), then clean up. Do NOT use `launchctl stop` alone
  — launchd will restart it immediately.

  1. launchctl unload ~/Library/LaunchAgents/com.robyx.bot.plist
  2. rm -f ~/Library/LaunchAgents/com.robyx.bot.plist
  3. pkill -f "python.*bot\.py" 2>/dev/null || true    # kill any orphan process
  4. (optional) rm -rf <project-root>/data/service-*.log <project-root>/bot.log*

  Or simply run: ./install/uninstall-mac.sh

Linux (systemd):
  The service has Restart=on-failure. You must stop AND disable the unit, then remove it.
  Do NOT use `systemctl stop` alone — systemd will restart it on the next failure or reboot.

  1. systemctl --user stop robyx
  2. systemctl --user disable robyx
  3. rm -f ~/.config/systemd/user/robyx.service
  4. systemctl --user daemon-reload
  5. pkill -f "python.*bot\.py" 2>/dev/null || true    # kill any orphan process

  Or simply run: ./install/uninstall-linux.sh

Windows (Task Scheduler):
  1. Stop-ScheduledTask -TaskName Robyx
  2. Unregister-ScheduledTask -TaskName Robyx -Confirm:$false
  3. Stop-Process -Name python* -Force -ErrorAction SilentlyContinue

  Or simply run: powershell install/uninstall-windows.ps1

IMPORTANT: The uninstall scripts handle all of the above automatically.
Prefer running the script over manual commands. The scripts do NOT delete the
project directory — only the service registration and runtime files (logs, locks, pid).

-->

### Step 2: Create a bot on your chosen platform

<details>
<summary><strong>Telegram</strong></summary>

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, pick a name and username
3. Copy the token — the setup wizard handles the rest (auto-detects chat ID and owner ID when you add the bot to a group)

</details>

<details>
<summary><strong>Discord</strong></summary>

1. **Enable Developer Mode in Discord** — this is required to copy Server ID and User ID in the steps below. Without it, the "Copy ID" options won't appear in right-click menus and you won't be able to complete the setup.
   - Open Discord → click the **gear icon** (⚙️) at the bottom left → **App Settings** → **Advanced** → toggle **Developer Mode** ON
2. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
3. Click **New Application** → name it (e.g. "Robyx")
4. Left menu → **Bot** → click **Reset Token** → copy the token
5. Scroll down → enable **Message Content Intent**
6. Left menu → **OAuth2** → URL Generator → select scope **bot**, then enable these permissions:
   - Send Messages
   - Manage Channels
   - Read Message History
   - Create Public Threads
   - Send Messages in Threads
7. Paste the token in the setup wizard — it generates the invite link for you
8. Open the invite link → select a server (or create one) → Authorize
9. If detection succeeds, the interactive setup auto-detects the server, creates `#control-room`, and finds your user ID; otherwise it falls back to asking for the IDs manually

</details>

<details>
<summary><strong>Slack</strong></summary>

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App
2. Choose **From scratch**, name it, select your workspace
3. **OAuth & Permissions** → add scopes: `chat:write`, `channels:manage`, `channels:read`, `files:read`
4. **Install to Workspace** → copy the Bot Token (`xoxb-...`)
5. **Basic Information** → App-Level Tokens → generate one with `connections:write` → copy (`xapp-...`)
6. **Socket Mode** → enable it

</details>

### Step 3: Install as system service

```bash
./install/install-mac.sh       # macOS (launchd)
./install/install-linux.sh     # Linux (systemd)
# powershell install/install-windows.ps1  # Windows
```

That's it. Open your messaging platform and start talking to Robyx.

---

<details>
<summary><strong>Project Structure</strong></summary>

```
robyx-ai/
├── setup.py                   # Setup wizard (interactive or CLI flags)
├── ORCHESTRATOR.md            # Robyx's behavior reference
├── AGENTS.md                  # Agent / specialist conventions
├── CHANGELOG.md               # Per-version summaries
├── docs/                      # Topical documentation (linked from README)
├── releases/                  # Full release notes per version
├── templates/
│   ├── SCHEDULER_AGENT.md     # Scheduler agent system-prompt template
│   ├── agent-template.md
│   └── specialist-template.md
├── VERSION                    # Current version
├── bot/                       # Python application
│   ├── _bootstrap.py          # Start-up dep sanity check (runs before imports)
│   ├── bot.py                 # Entry point + service jobs
│   ├── config.py              # All config from .env + system prompts
│   ├── config_updates.py      # Direct KEY=value .env updates from chat
│   ├── agents.py              # Agent model & session manager
│   ├── ai_backend.py          # AI backend abstraction
│   ├── ai_invoke.py           # CLI invocation, streaming, response patterns
│   ├── handlers.py            # Command & message handlers (platform-agnostic)
│   ├── scheduler.py           # Unified scheduler (reminders, one-shot, periodic, continuous)
│   ├── continuous.py          # Continuous task state management
│   ├── scheduled_delivery.py  # Output relay from scheduled runs to topics
│   ├── task_runtime.py        # Agent context resolver for scheduled tasks
│   ├── memory.py              # Agent memory system
│   ├── model_preferences.py   # Backend-aware model alias resolution
│   ├── topics.py              # Workspace/channel creation
│   ├── media.py               # Outgoing image compression (Pillow)
│   ├── migrations/            # Migration framework (chain + legacy registry)
│   │   ├── base.py            # Migration / MigrationContext / version utils
│   │   ├── runner.py          # Chain discovery + execution
│   │   ├── tracker.py         # data/migrations.json persistence
│   │   ├── legacy.py          # Pre-0.20.12 name-keyed registry
│   │   └── vX_Y_Z.py          # One per release, continuous chain
│   ├── session_lifecycle.py   # Session invalidation logic
│   ├── updater.py             # Auto-update system (snapshots + smoke test)
│   ├── process.py             # Subprocess management
│   ├── voice.py               # Voice transcription (Whisper)
│   ├── i18n.py                # UI strings
│   └── messaging/             # Platform adapters
│       ├── base.py            #   Platform ABC + PlatformMessage dataclass
│       ├── telegram.py        #   Telegram adapter
│       ├── discord.py         #   Discord adapter
│       └── slack.py           #   Slack adapter
├── install/                   # Platform installers
├── scripts/                   # Dev tooling (e.g. new_migration.py)
├── tests/                     # Test suite (960+ tests)
└── data/                      # Runtime data (gitignored, created on first boot)
    ├── bot.pid                # Single-instance lock
    ├── state.json             # Agent state persistence
    ├── queue.json             # Unified scheduler queue (all task types)
    ├── tasks.md               # Workspace registry (auto-managed)
    ├── specialists.md         # Specialist registry (auto-managed)
    ├── agents/                # Workspace agent briefs (.md)
    ├── specialists/           # Specialist briefs (.md)
    ├── continuous/            # Continuous task state files
    ├── migrations.json        # Applied migrations tracker
    ├── backups/               # Pre-update tar snapshots (retention: 3)
    └── memory/                # Robyx & specialist memory
```

</details>

---

## License

[MIT](LICENSE) — Roberto Sartori
