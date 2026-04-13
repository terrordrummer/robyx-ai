<div align="center">

<img src="robyx-ai.png" alt="Robyx" width="400">

# Robyx

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

## Table of Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [The Three Roles](#the-three-roles)
- [Workspaces](#workspaces)
- [The Scheduler](#the-scheduler)
- [Memory System](#memory-system)
- [Building Your Team](#building-your-team)
- [Configuration](#configuration)
- [Commands](#commands)
- [AI Backends](#ai-backends)
- [Voice Messages](#voice-messages)
- [Receiving Images](#receiving-images)
- [Platform Migration](#platform-migration)
- [Auto-Updates](#auto-updates)
- [Service Management](#service-management)
- [Project Structure](#project-structure)
- [License](#license)

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

## How It Works

You talk to **Robyx** in **Headquarters** — the control channel where the orchestrator lives. Robyx understands your requests, creates the right agents, and coordinates everything.

```
You:   "Create a workspace to monitor BTC price every hour, alert me below 60k"
Robyx:  Creates a scheduled workspace. Agent checks price hourly,
       sends alerts to its dedicated topic/channel.

You:   "I need a code reviewer that knows our Python conventions"
Robyx:  Creates a cross-functional specialist. Available to all
       workspaces via @code-reviewer.

You:   "Remind me Thursday at 9am — dentist appointment"
Robyx:  Schedules a [REMIND] entry. The Python reminder engine fires
       at the exact minute, survives bot restarts, no LLM needed.
```

Reminders are a **universal skill**: any agent in Robyx — Robyx, workspaces, specialists, and focused-mode agents — can schedule one with the `[REMIND ...]` pattern. The bot parses the pattern, queues it into `data/reminders.json`, and the engine delivers the message at the exact time. See **Reminders** in `ORCHESTRATOR.md` for the attribute reference.

Every agent lives in its own topic/channel. You can talk to any agent directly by opening it, or use `/focus <name>` to redirect all messages to that agent.

---

## The Three Roles

Robyx has three types of agents, each with a distinct purpose:

```
                        ┌──────────────────────────┐
                        │          YOU              │
                        │      (Chat messages)      │
                        └────────────┬─────────────┘
                                     │
                                     ▼
                        ┌──────────────────────────┐
                        │         KAEL              │
                        │  Principal Orchestrator   │
                        │  Lives in Headquarters   │
                        │  Creates & manages all    │
                        │  agents and workspaces    │
                        └──┬─────────┬──────────┬──┘
                           │         │          │
              ┌────────────▼──┐  ┌───▼────────┐ │
              │  WORKSPACE    │  │ WORKSPACE   │ │
              │  Agent        │  │ Agent       │ │
              │               │  │             │ │
              │ One channel.  │  │ One channel.│ │
              │ One job.      │  │ One job.    │ │
              │ Focused.      │  │ Focused.    │ │
              └──────┬────────┘  └──────┬──────┘ │
                     │                  │        │
                     │   ┌──────────────▼────┐   │
                     └──►│   SPECIALIST      │◄──┘
                         │   Cross-functional│
                         │                   │
                         │ Available to ALL   │
                         │ workspaces via     │
                         │ @mention           │
                         └───────────────────┘
```

### Robyx — The Orchestrator

Robyx is your single point of contact. It lives in **Headquarters** — the control channel of Robyx — and handles:

- **Creating workspaces** when you describe a task or project
- **Spawning specialists** when cross-functional expertise is needed
- **Delegating work** to the right agent
- **Managing focus** — routing your messages to the correct agent
- **Coordinating** the overall team

You never need to configure agents manually. Just describe what you need, and Robyx builds it.

**Headquarters is coordination-only.** Robyx treats the control channel as a dispatch point, not a workbench. Fleet status, workspace creation, delegation, and meta-operations belong in Headquarters; real project work (R&D iterations, builds, deploys, feature implementation) belongs in the workspace topic/channel of the project that owns it. When a request implies deep work inside a specific project, Robyx offers `[DELEGATE @agent: ...]` or suggests switching to the workspace topic/channel — it does not silently start executing from Headquarters.

### Workspace Agents — The Workers

Each workspace is its own **topic/channel** with a **dedicated AI agent**. The agent:

- Has its own conversation history (persistent sessions)
- Runs in its stored `work_dir` on your machine
- Follows custom instructions written by Robyx (or by you)
- Can request help from specialists

New workspaces inherit the configured `KAELOPS_WORKSPACE` as their initial `work_dir`.
Robyx does not currently auto-discover a different filesystem path for each workspace.

Workspace agents come in three types:

| Type | Behavior | Example |
|------|----------|---------|
| `interactive` | Responds when you message its topic/channel | A project assistant |
| `scheduled` | Runs on a timer (hourly, daily, etc.) | A system monitor |
| `one-shot` | Runs once at a specific time via the timed queue | A scheduled R&D iteration |

> Note: simple "ping me at T" reminders no longer need a `one-shot` workspace. Any agent can schedule them directly with the universal `[REMIND ...]` skill — use `one-shot` only when you actually need an agent re-invoked at a specific time to *do work*.

### Specialists — The Experts

Specialists are **horizontal agents** that serve all workspaces. Think of them as team-wide resources:

- A **code reviewer** that any workspace can ask for a review
- A **deployer** that knows your infrastructure
- A **researcher** that can deep-dive into any topic

Any workspace agent can call a specialist with `@name`. The specialist responds in the requesting workspace's topic/channel, keeping context local.

---

## Workspaces

A workspace is the fundamental unit of Robyx. When Robyx creates one, this is what happens:

```
1. Topic/channel created           →  #btc-monitor
                                       (forum topic on Telegram,
                                        channel on Discord/Slack)
2. Agent instructions generated    →  data/agents/btc-monitor.md
3. Scheduler entry written         →  interactive/scheduled: data/tasks.md
                                       one-shot: data/timed_queue.json
4. Data directory created          →  data/btc-monitor/
5. Agent activated                 →  ready to work
```

### Lifecycle

```
    You ask Robyx ──→ [Created] ──→ [Active] ──→ [Closed]
                                      │
                                      ▼
                                   [Paused]
                                   (scheduler
                                    skips it)
```

- **Active** — agent works, responds, and maintains its state
- **Paused** — agent stops; you can resume anytime
- **Closed** — the platform topic/channel is archived or closed; the agent is removed

### Talking to Workspaces

Three ways to interact with a workspace agent:

1. **Open its topic/channel** — messages go directly to that agent
2. **@mention** — write `@agent-name do something` from any channel
3. **Focus mode** — `/focus agent-name` routes ALL your messages to that agent until you say "back to Robyx"

---

## The Scheduler

Robyx uses two automation loops:

- The periodic scheduler (`SCHEDULER_INTERVAL`, default `600`) reads `data/tasks.md` and dispatches enabled `scheduled` workspaces. `interactive` rows stay user-triggered.
- The timed scheduler (`TIMED_SCHEDULER_INTERVAL`, default `60`) reads `data/timed_queue.json` for one-shot workspaces, timed periodic runs, and `[REMIND agent="..."]` actions, and reads `data/reminders.json` for plain text reminders.

Both automation paths follow the same runtime contract:

- They spawn independent AI CLI processes and exit immediately.
- They use PID lock files under `data/<task>/lock` to prevent duplicate runs and clean up stale locks.
- They execute in the target agent's stored `work_dir`.
- They keep raw output in per-task logs and relay the parsed result back into the target topic/channel, so logs remain operational artifacts rather than the only delivery path.

One-shot workspaces live in `data/timed_queue.json`. After they fire, the timed queue marks them `dispatched`; closing a workspace also cancels any still-pending timed rows that target it.

---

## Memory System

Agents need to remember context between conversations. Robyx has a two-tier memory system:

### Active Memory

A compact document (~5000 words max) loaded into the agent's context at the start of every conversation. Contains:

- Current state of the project/task
- Active decisions and the reasoning behind them
- Open TODOs
- Known issues and gotchas

Agents update their active memory **continuously** — not at session boundaries. A decision is made? Write it now. A TODO is completed? Update immediately.

### Archive

When information becomes obsolete (completed TODO, superseded decision), agents move it from active memory to a quarterly archive file. The archive is not loaded by default — it's queryable on demand when historical context is needed.

### Integration with existing projects

Robyx respects your existing setup:

| Project state | Memory behavior |
|---------------|-----------------|
| Has Claude Code memory (`.claude/`, `CLAUDE.md`) | Robyx doesn't interfere — native memory works as-is |
| No existing memory | Robyx creates `.robyx/memory/` with active + archive |
| Robyx and specialists | Always use `data/memory/{name}/` |

This means you can work on a project **both directly** (terminal + Claude Code) **and via Robyx** (chat) without memory conflicts.

---

## Building Your Team

This is the core of Robyx: **you build your team through conversation**.

### Start with Robyx

Tell Robyx what you need in natural language:

```
"I need a React app workspace, a Python API workspace, and an infrastructure workspace.
Create one for each."
```

Robyx creates three workspace topics/channels and three agents with appropriate instructions. By default,
each new workspace inherits the configured `KAELOPS_WORKSPACE` as its starting
`work_dir`; Robyx does not auto-map each workspace to a separate project directory.

### Add Specialists

As you work, you'll notice patterns — tasks that cut across projects:

```
"Create a code reviewer specialist that checks for security issues
and follows our team's Python conventions."
```

Now any workspace agent can call `@code-reviewer` when it needs a review.

### Evolve Over Time

Your team grows organically:

```
Week 1:  Robyx + 2 project workspaces
Week 2:  + code reviewer specialist
Week 3:  + system monitor (scheduled, runs every 6h)
Week 4:  + deployment specialist that knows your Cloudflare setup
Month 2: + research workspace for ML experiments
         + data pipeline monitor
```

Each agent has its own memory, its own instructions, and its own topic/channel. You interact with them like colleagues — assign tasks, ask questions, review their work.

### Why This Approach

Pre-built agent platforms give you 500 skills you didn't ask for and charge you for the complexity. Robyx gives you:

- **Zero skill bloat** — every agent does exactly what you defined
- **Your vocabulary** — agents speak your domain language because you trained them
- **Your workflow** — no adapting to someone else's idea of how work should flow
- **Full transparency** — agent instructions are markdown files you can read and edit
- **No lock-in** — swap AI backends with one env var; everything is files on disk

---

## Configuration

All settings live in `.env` (see [`.env.example`](.env.example)).

Robyx always parses the compatibility keys `KAELOPS_BOT_TOKEN`, `KAELOPS_CHAT_ID`, and `KAELOPS_OWNER_ID` at startup. On Telegram they are real values. On Slack and Discord the installer writes harmless placeholder values so the shared config loader still boots; if you maintain `.env` by hand, keep the placeholder examples from [`.env.example`](.env.example).

### Common

| Variable | Required | Description |
|----------|:--------:|-------------|
| `KAELOPS_PLATFORM` | Yes | `telegram` / `discord` / `slack` |
| `AI_BACKEND` | Yes | `claude` / `codex` / `opencode` |
| `AI_CLI_PATH` | — | Custom CLI path (auto-detected if on `PATH`) |
| `CLAUDE_PERMISSION_MODE` | — | Advanced Claude-only override. Leave unset to keep Claude Code's default permission prompts; set explicitly only if you want a non-default mode such as `bypassPermissions` |
| `KAELOPS_WORKSPACE` | — | Default `work_dir` inherited by newly created workspaces and specialists (default: `~/Workspace`) |
| `OPENAI_API_KEY` | — | For voice message transcription (Whisper) |
| `SCHEDULER_INTERVAL` | — | Scheduler check interval in seconds (default: `600`) |
| `TIMED_SCHEDULER_INTERVAL` | — | Timed queue / reminder check interval in seconds (default: `60`) |
| `UPDATE_CHECK_INTERVAL` | — | Auto-update check interval in seconds (default: `3600`) |

### Telegram

| Variable | Required | Description |
|----------|:--------:|-------------|
| `KAELOPS_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `KAELOPS_CHAT_ID` | Yes | Supergroup chat ID (negative number) |
| `KAELOPS_OWNER_ID` | Yes | Your Telegram user ID |

### Discord

| Variable | Required | Description |
|----------|:--------:|-------------|
| `DISCORD_BOT_TOKEN` | Yes | Bot token from discord.com/developers/applications |
| `DISCORD_GUILD_ID` | Yes | Server ID (right-click server → Copy Server ID) |
| `DISCORD_OWNER_ID` | Yes | Your Discord user ID |
| `DISCORD_CONTROL_CHANNEL_ID` | Yes | Control-room channel ID. The interactive setup usually discovers or creates it for you; manual `.env` or non-interactive setup should set it explicitly |

### Slack

| Variable | Required | Description |
|----------|:--------:|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-Level Token (`xapp-...`) with `connections:write` |
| `SLACK_CHANNEL_ID` | Yes | Control-room channel ID |
| `SLACK_OWNER_ID` | Yes | Your Slack user ID |

You can update settings in chat with explicit assignments like `OPENAI_API_KEY=sk-...` or `AI_BACKEND=codex`. Recognized `KEY=value` messages are applied directly to `.env` and restarted without sending the value through the AI backend.

---

## Commands

Telegram and Discord support the slash commands below. On Slack, use natural language in the control room instead; setup does not register native Slack slash commands.

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/workspaces` | List active workspaces with status |
| `/specialists` | List cross-functional agents |
| `/status` | System overview — agents, focus, activity |
| `/focus <name\|off>` | Talk directly to an agent (bypass Robyx) |
| `/reset <name>` | Reset an agent's session (fresh conversation) |
| `/ping` | Check if the bot is alive |
| `/checkupdate` | Check for new Robyx versions |
| `/doupdate` | Apply a pending update |

Commands are just shortcuts. Most interaction is **natural language** — talk to Robyx like a colleague.

---

## AI Backends

Robyx is a thin orchestration layer on top of CLI-based AI tools:

| Backend | CLI | Sessions | Streaming | Config |
|---------|-----|:--------:|:---------:|--------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` | Yes | Yes | `AI_BACKEND=claude` |
| [Codex CLI](https://github.com/openai/codex) | `codex` | — | — | `AI_BACKEND=codex` |
| [OpenCode](https://github.com/opencode-ai/opencode) | `opencode` | Yes | — | `AI_BACKEND=opencode` |

Adding a new backend is one class in [`ai_backend.py`](bot/ai_backend.py) — implement `build_command()` and `parse_response()`.

When using Claude Code, responses are **streamed in real-time**. Agents can emit `[STATUS ...]` markers that appear instantly in chat, so you see progress instead of just "typing...".

Robyx no longer forces Claude Code into `bypassPermissions` by default. If you want a non-default Claude permission mode, set `CLAUDE_PERMISSION_MODE=...` explicitly in `.env`. Leaving it blank preserves Claude Code's own interactive safety behaviour.

OpenCode runs with `--format json` and resumes its native session via `--session ses_…` so multi-turn conversations stay coherent across messages and bot restarts. Robyx captures the session id from the CLI output on the first turn and replays it automatically on every subsequent turn.

### Model preferences (`models.yaml`)

Workspaces, specialists, and scheduled tasks express their model intent as a **semantic alias** (`fast` / `balanced` / `powerful`) or as a **role** (`orchestrator` / `workspace` / `specialist` / `scheduled` / `one-shot`). Robyx resolves the alias at invocation time into the concrete model id understood by the active backend, using the table at the repo root in [`models.yaml`](models.yaml):

```yaml
defaults:
  orchestrator: balanced
  workspace: balanced
  specialist: powerful
  scheduled: fast
  one-shot: fast

aliases:
  fast:
    claude: haiku
    codex: gpt-5-mini
    opencode: openai/gpt-5-mini
  balanced:
    claude: sonnet
    codex: gpt-5
    opencode: openai/gpt-5
  powerful:
    claude: opus
    codex: gpt-5.4
    opencode: openai/gpt-5.4
```

This is especially useful with `opencode`, which requires provider-qualified names like `openai/gpt-5`. With `models.yaml` you write `model="balanced"` once in `data/tasks.md` and the right id reaches the right backend.

If `models.yaml` is missing, Robyx falls back to the legacy `AI_MODEL_DEFAULTS` / `AI_MODEL_ALIASES` env vars (JSON-encoded), then to the hard-coded defaults baked into `bot/config.py`. Old `data/tasks.md` rows that still say `haiku` / `sonnet` / `opus` keep working — those are silently mapped onto `fast` / `balanced` / `powerful` by the resolver. Power users can also pass an explicit backend model id (e.g. `model="openai/gpt-5.4-preview"`) and Robyx will pass it through unchanged.

---

## Voice Messages

Send a voice message to any channel. Robyx will:

1. Transcribe it using OpenAI Whisper
2. Show you the transcription (so you can see what was said without replaying)
3. Route the transcribed text to the appropriate agent

If `OPENAI_API_KEY` is not configured, the bot replies with a clear message explaining what's missing and how to fix it. You can also send `OPENAI_API_KEY=sk-...` in chat and Robyx will update `.env` locally and restart.

---

## Receiving Images

Any workspace agent can send you an image file when you explicitly ask for one. Just ask in plain language — *"mandami il risultato dell'ultima iterazione"*, *"show me the benchmark output"*, *"send me the latest render"* — and the agent will deliver the file to the chat.

Under the hood the agent emits a `[SEND_IMAGE path="..." caption="..."]` tag in its reply; Robyx intercepts it, runs the file through an auto-compression pipeline (JPEG re-encoding with progressive quality and downscale fallback) if it exceeds the platform's upload cap, and uploads it via the native photo API of each messaging platform (`sendPhoto` on Telegram, `channel.send(file=...)` on Discord, `files_upload_v2` on Slack).

**Strict rule enforced in the agent system prompt**: agents only emit `[SEND_IMAGE]` when the user has explicitly asked to see, send, or share an image. They never attach images proactively, as a bonus, or because the conversation merely touched on an image.

---

## Platform Migration

Switch between Telegram, Discord, and Slack at any time. Just tell Robyx:

> "Passa a Discord" / "Switch to Slack" / "Migrate to Telegram"

Robyx will guide you through the target platform's required credentials and IDs, update the configuration, and restart. Telegram needs a bot token plus chat and owner IDs; Slack needs bot and app tokens plus control-room and owner IDs; Discord needs a bot token plus guild, control-room, and owner IDs. All workspaces, agents, scheduled tasks, and memory are preserved — only the messaging transport changes.

---

## Auto-Updates

Robyx checks for new versions every hour:

- **Safe updates** (non-breaking, compatible) are applied automatically — pull, install Python dependencies, run post-update migrations, restart
- **Breaking updates** notify you and require manual `/doupdate`
- If anything fails, it rolls back automatically to the previous version tag
- Use `/checkupdate` for an immediate check

### Dependency safety net

Auto-update is rigorous about Python dependencies:

- `apply_update` runs `pip install -r bot/requirements.txt` with full logging, return-code checking, and a 10-minute timeout. A non-zero pip exit rolls the update back to the previous version and reports the pip error in chat — no silent failures.
- A startup bootstrap check (`bot/_bootstrap.py`) runs at the top of every bot start-up. It hashes `requirements.txt` against a marker stored inside the venv and reruns `pip install` if they differ. This covers manual pulls, crashed updates, and corrupted venvs — any boot with stale deps self-heals before `import`s run.

### Post-update migrations

A tiny migration framework (`bot/migrations.py`) runs post-update instructions exactly once per deployment on the next boot after an update. Each migration is a registered async function tracked in `data/migrations.json` and never retried once attempted, so an unsatisfiable migration never blocks the boot. The framework is how Robyx performs operations like renaming the main channel on the platform (e.g. *General* → *Headquarters*) after an upgrade.

### Agent session lifecycle on updates

The Claude Code CLI bakes the system prompt into a session at creation time and ignores `--append-system-prompt` on `--resume`. So whenever a release modifies a system prompt or an agent brief, the affected agents must start a fresh session for the new instructions to actually take effect.

Since v0.15.1 this is **automatic and structural**, and since v0.15.2 it's **also correct in production**. After a successful `git pull`, `apply_update` computes `git diff --name-only <previous>..HEAD` and hands the changed paths to `bot/session_lifecycle.py:invalidate_sessions_via_manager`, which routes the actual reset through the live `AgentManager.reset_sessions(...)` method:

- A change to `bot/config.py` (the system prompts) or `bot/ai_invoke.py` (the per-agent brief loader) resets **every** agent.
- A change to repo-managed `agents/<name>.md` resets only **that** workspace agent.
- A change to repo-managed `specialists/<name>.md` resets only **that** specialist.
- Anything else (Python logic, tests, README, releases) is correctly ignored — those changes are picked up by the process restart that follows `apply_update`.

**Why "via the manager" matters**: in v0.15.0 and v0.15.1 the reset was implemented as a direct write to `data/state.json`. The running bot's `AgentManager` held the pre-mutation copy in memory and the very next `save_state()` call from any interaction silently overwrote the reset. The migration was tracked as `success` but the agents kept running with the old prompt forever. v0.15.2 fixes this structurally by going through `AgentManager.reset_sessions(...)`, which mutates the in-memory copy and persists in a single atomic step. **`state.json` is never mutated outside the AgentManager**.

The progress callback emits `Reset AI sessions for N agent(s): name1, name2` so the side effect is visible inline in the boot summary on Telegram. Failures here are logged but never block the update — the restart still happens. Release authors no longer need to write per-release session-reset migrations: the contract is anchored in the updater itself.

---

## Service Management

<details>
<summary><strong>macOS (launchd)</strong></summary>

```bash
./install/install-mac.sh              # Install
launchctl start com.robyx.bot       # Start
launchctl stop com.robyx.bot        # Stop (temporary — KeepAlive restarts it)
./install/uninstall-mac.sh            # Uninstall (stops + removes service)
```

The service runs at login with `KeepAlive` enabled — if it crashes or is killed, launchd restarts it automatically. To **permanently stop** the service, use `uninstall-mac.sh` or run `launchctl unload ~/Library/LaunchAgents/com.robyx.bot.plist` (this removes both the keep-alive and the process). Simply killing the process or using `launchctl stop` will only stop it temporarily.

</details>

<details>
<summary><strong>Linux (systemd)</strong></summary>

```bash
./install/install-linux.sh            # Install
systemctl --user start robyx        # Start
systemctl --user stop robyx         # Stop (temporary — Restart=on-failure may restart it)
./install/uninstall-linux.sh          # Uninstall (stops + disables + removes service)
```

The service has `Restart=on-failure` — systemd restarts it after crashes. To **permanently stop**, use `uninstall-linux.sh` or run `systemctl --user disable --now robyx`.

</details>

<details>
<summary><strong>Windows (Task Scheduler)</strong></summary>

```powershell
powershell install/install-windows.ps1          # Install
Start-ScheduledTask -TaskName Robyx           # Start
Stop-ScheduledTask -TaskName Robyx            # Stop
powershell install/uninstall-windows.ps1        # Uninstall (stops + removes task)
```

</details>

A **PID file** (`data/bot.pid`) ensures only one instance runs at a time. If you accidentally start the bot twice, the second instance exits immediately.

---

<details>
<summary><strong>Project Structure</strong></summary>

```
robyx-ai/
├── setup.py                   # Setup wizard (interactive or CLI flags)
├── ORCHESTRATOR.md            # Robyx's behavior reference
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
│   ├── scheduler.py           # Periodic task scheduler loop
│   ├── timed_scheduler.py     # One-shot & timed task queue (60-sec loop)
│   ├── scheduled_delivery.py  # Output relay from scheduled runs to topics
│   ├── task_runtime.py        # Agent context resolver for scheduled tasks
│   ├── reminders.py           # Cross-platform text reminder engine
│   ├── memory.py              # Agent memory system
│   ├── model_preferences.py   # Backend-aware model alias resolution
│   ├── topics.py              # Workspace/channel creation
│   ├── media.py               # Outgoing image compression (Pillow)
│   ├── migrations.py          # Post-update migration framework
│   ├── session_lifecycle.py   # Session invalidation logic
│   ├── updater.py             # Auto-update system
│   ├── process.py             # Subprocess management
│   ├── voice.py               # Voice transcription (Whisper)
│   ├── i18n.py                # UI strings
│   └── messaging/             # Platform adapters
│       ├── base.py            #   Platform ABC + PlatformMessage dataclass
│       ├── telegram.py        #   Telegram adapter
│       ├── discord.py         #   Discord adapter
│       └── slack.py           #   Slack adapter
├── templates/                 # Agent & specialist writing guides
├── install/                   # Platform installers
├── releases/                  # Release notes per version
├── tests/                     # Test suite (900+ tests)
└── data/                      # Runtime data (gitignored, created on first boot)
    ├── bot.pid                # Single-instance lock
    ├── state.json             # Agent state persistence
    ├── tasks.md               # Workspace registry (auto-managed)
    ├── specialists.md         # Specialist registry (auto-managed)
    ├── agents/                # Workspace agent briefs (.md)
    ├── specialists/           # Specialist briefs (.md)
    ├── migrations.json        # Applied migrations tracker
    └── memory/                # Robyx & specialist memory
```

</details>

---

## License

[MIT](LICENSE) — Roberto Sartori
