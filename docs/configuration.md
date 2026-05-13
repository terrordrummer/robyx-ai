# Configuration

← [Back to README](../README.md)

All settings live in `.env` (see [`.env.example`](../.env.example)).

All env vars use the `ROBYX_` prefix. Legacy `KAELOPS_` prefixes are still accepted for backward compatibility. On Telegram the bot token, chat ID, and owner ID are real values. On Slack and Discord the installer writes harmless placeholder values so the shared config loader still boots; if you maintain `.env` by hand, keep the placeholder examples from [`.env.example`](../.env.example).

## Common

| Variable | Required | Description |
|----------|:--------:|-------------|
| `ROBYX_PLATFORM` | Yes | `telegram` / `discord` / `slack` (legacy `KAELOPS_PLATFORM` also accepted) |
| `AI_BACKEND` | Yes | `claude` / `codex` / `opencode` |
| `AI_CLI_PATH` | — | Custom CLI path (auto-detected if on `PATH`) |
| `CLAUDE_PERMISSION_MODE` | — | Claude Code permission mode (default: `bypassPermissions` for autonomous operation). Override to a different mode if needed. Note: on systems with enterprise MDM settings that set `permissions.disableBypassPermissionsMode: disable`, this override is enforced by Claude and cannot be relaxed from Robyx. |
| `CODEX_APPROVAL_POLICY` | — | Codex approval policy (default: `never` — no prompts). Override with `untrusted` / `on-request` / `on-failure` for stricter approvals. |
| `CODEX_SANDBOX` | — | Codex sandbox policy (default: `danger-full-access` — no sandbox). Override with `read-only` / `workspace-write` for stricter isolation. |
| `OPENCODE_PERMISSION` | — | OpenCode global permission level (default: `allow`). Set to `ask` or `deny` for stricter policies. Robyx writes a managed `opencode-managed.json` config at boot and points OpenCode at it via `OPENCODE_CONFIG`, unless `OPENCODE_CONFIG` is already set. |
| `ROBYX_WORKSPACE` | — | Default `work_dir` inherited by newly created workspaces and specialists (default: `~/Workspace`). Legacy `KAELOPS_WORKSPACE` is also accepted. |
| `OPENAI_API_KEY` | — | For voice message transcription (Whisper) |
| `SCHEDULER_INTERVAL` | — | Scheduler check interval in seconds (default: `60`) |
| `UPDATE_CHECK_INTERVAL` | — | Auto-update check interval in seconds (default: `3600`) |
| `REMINDER_MAX_AGE_SECONDS` | — | Reminders whose `fire_at` is older than this limit are marked `failed` with `failure_reason="expired"` instead of retrying forever (default: `604800` = 7 d, raised from 24 h in v0.20.28). |
| `CLAIM_TIMEOUT_SECONDS` | — | Stale-claim reset timeout for reminders and scheduled tasks (default: `600` = 10 min). A slower delivery watcher now has more room before the scheduler decides the claim is stuck. |
| `SMOKE_TEST_TIMEOUT_SECONDS` | — | Post-update smoke-test timeout (default: `60`). Raise on slow machines / cold caches to avoid false-positive rollbacks. |
| `VOICE_TIMEOUT_SECONDS` | — | Voice (Whisper) transcription HTTP timeout (default: `60`). |
| `AI_IDLE_TIMEOUT` | — | Max seconds the AI subprocess may stay silent (no stream-json output) before Robyx considers it hung and kills it (default: `600` = 10 min). A responsive agent that keeps emitting lines stays alive indefinitely up to `AI_TIMEOUT`. Only applies to the streaming path (Claude Code, OpenCode). |
| `AI_TIMEOUT` | — | Hard wall-clock cap per AI invocation in seconds (default: `7200` = 2 h). Safety net for runaway processes and the sole timeout on the non-streaming path. On streaming backends, prefer tuning `AI_IDLE_TIMEOUT` — this is rarely hit when the agent is actually producing output. |

## Telegram

| Variable | Required | Description |
|----------|:--------:|-------------|
| `ROBYX_BOT_TOKEN` | Yes | Bot token from @BotFather (legacy `KAELOPS_BOT_TOKEN` also accepted) |
| `ROBYX_CHAT_ID` | Yes | Supergroup chat ID (negative number) (legacy `KAELOPS_CHAT_ID` also accepted) |
| `ROBYX_OWNER_ID` | Yes | Your Telegram user ID (legacy `KAELOPS_OWNER_ID` also accepted) |

## Discord

| Variable | Required | Description |
|----------|:--------:|-------------|
| `DISCORD_BOT_TOKEN` | Yes | Bot token from discord.com/developers/applications |
| `DISCORD_GUILD_ID` | Yes | Server ID (right-click server → Copy Server ID) |
| `DISCORD_OWNER_ID` | Yes | Your Discord user ID |
| `DISCORD_CONTROL_CHANNEL_ID` | Yes | Control-room channel ID. The interactive setup usually discovers or creates it for you; manual `.env` or non-interactive setup should set it explicitly |
| `DISCORD_INVITE_TTL_DAYS` | No (default `7`) | Spec 007 — TTL of the invite link Robyx generates when a collaborative-workspace guild is bound. `0` = no expiry (Discord sentinel). Invalid or negative values fall back to the default with a WARN log. |
| `DISCORD_INVITE_MAX_USES` | No (default `10`) | Spec 007 — usage cap on generated invite links. `0` = unlimited (Discord sentinel). Invalid or negative values fall back to the default. |

**Required Discord bot OAuth permissions** for collaborative workspaces (spec 007 — set on the bot invite URL):

| Permission | Why |
|---|---|
| `Send Messages` | Post responses in the bound channel and in HQ. |
| `Manage Channels` | Rename topics/threads when continuous-task state markers change (spec 006); needed for forum-topic operations. |
| `Create Instant Invite` | Generate the invite URL Robyx attaches to the HQ notification when binding a workspace. |
| `View Audit Log` | Resolve "who added me" on `on_guild_join`. Without this permission the bot falls back to the `/im-the-owner <workspace-name>` manual claim — workflows still work, but each Flow A bind requires a manual step. |

## Slack

| Variable | Required | Description |
|----------|:--------:|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | App-Level Token (`xapp-...`) with `connections:write` |
| `SLACK_CHANNEL_ID` | Yes | Control-room channel ID |
| `SLACK_OWNER_ID` | Yes | Your Slack user ID |

You can update settings in chat with explicit assignments like `OPENAI_API_KEY=sk-...` or `AI_BACKEND=codex`. Recognized `KEY=value` messages are applied directly to `.env` and restarted without sending the value through the AI backend.

## Platform Migration

Switch between Telegram, Discord, and Slack at any time. Just tell Robyx:

> "Passa a Discord" / "Switch to Slack" / "Migrate to Telegram"

Robyx will guide you through the target platform's required credentials and IDs, update the configuration, and restart. Telegram needs a bot token plus chat and owner IDs; Slack needs bot and app tokens plus control-room and owner IDs; Discord needs a bot token plus guild, control-room, and owner IDs. All workspaces, agents, scheduled tasks, and memory are preserved — only the messaging transport changes.

---

← [Back to README](../README.md)
