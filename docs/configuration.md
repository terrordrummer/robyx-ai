# Configuration

← [Back to README](../README.md)

All settings live in `.env` (see [`.env.example`](../.env.example)).

## Local secret and permission policy

On POSIX, setup writes `.env` atomically as `0600`; `data/`, its subdirectories,
and runtime files are maintained as `0700`/`0600`. The service starts with
umask `0077`, and an idempotent pre-config boot hardener repairs existing
installations after manual pulls or upgrades. It never follows symlinks. If an
existing path cannot be secured (including a symlinked `.env`/`data` root),
startup fails before `.env` is loaded. Symlinks and non-regular entries anywhere
inside the private runtime tree are never followed. Windows keeps atomic writes
and current-user service isolation, but
Python's standard library has no portable equivalent for POSIX modes/ACLs, so
the mode-hardening pass is a documented no-op there.

Interactive setup reads platform tokens and the optional OpenAI key without
terminal echo. For non-interactive setup, prefer `--bot-token-file`,
`--slack-bot-token-file`, `--slack-app-token-file`,
`--discord-bot-token-file`, and `--openai-key-file`. On POSIX these inputs must
be regular, non-symlink, current-user-owned files with no group/other access.
The equivalent setup-only environment variables are
`ROBYX_SETUP_BOT_TOKEN`, `ROBYX_SETUP_SLACK_BOT_TOKEN`,
`ROBYX_SETUP_SLACK_APP_TOKEN`, `ROBYX_SETUP_DISCORD_BOT_TOKEN`, and
`ROBYX_SETUP_OPENAI_KEY`. Legacy value-bearing CLI flags remain compatible but
emit a deprecation warning because argv can appear in shell history and process
lists.

Credential, owner/chat identity, participant policy, and backend sandbox
assignments sent through chat are rejected locally before AI routing. Robyx
logs and replies with the key name only; the submitted value is neither changed
nor forwarded to the backend.

All env vars use the `ROBYX_` prefix. Legacy `KAELOPS_` prefixes are still accepted for backward compatibility. On Telegram the bot token, chat ID, and owner ID are real values. On Slack and Discord the installer writes harmless placeholder values so the shared config loader still boots; if you maintain `.env` by hand, keep the placeholder examples from [`.env.example`](../.env.example).

## Common

| Variable | Required | Description |
|----------|:--------:|-------------|
| `ROBYX_PLATFORM` | Yes | `telegram` / `discord` / `slack` (legacy `KAELOPS_PLATFORM` also accepted) |
| `AI_BACKEND` | Yes | `claude` / `codex` / `opencode` |
| `AI_CLI_PATH` | — | Custom CLI path (auto-detected if on `PATH`) |
| `CLAUDE_PERMISSION_MODE` | — | Claude Code permission mode. Accepted explicit values: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`; blank uses the backend default. Note: enterprise MDM policy can enforce a stricter mode and cannot be relaxed by Robyx. |
| `CODEX_APPROVAL_POLICY` | — | Codex approval policy (default: `never` — no prompts). Override with `untrusted` / `on-request` / `on-failure` for stricter approvals. |
| `CODEX_SANDBOX` | — | Codex sandbox policy (default: `danger-full-access` — no sandbox). Override with `read-only` / `workspace-write` for stricter isolation. |
| `OPENCODE_PERMISSION` | — | OpenCode executive/system permission level (default: `allow`). Set to `ask` or `deny` for stricter policies. Before the first privileged invocation Robyx writes a managed `opencode-managed.json` and passes it only in that child process's `OPENCODE_CONFIG`, unless the variable was already set by the operator. Participant turns receive a separate child-only deny config. |
| `COLLAB_PARTICIPANT_POLICY` | — | Local-only collaborative participant policy: `disabled` (default) or `read-only`. It is intentionally not chat-editable. Opt in to `read-only` only when every participant may inspect all workspace-readable content: it protects integrity, not confidentiality. |
| `ROBYX_WORKSPACE` | — | Default `work_dir` inherited by newly created workspaces and specialists (default: `~/Workspace`). Legacy `KAELOPS_WORKSPACE` is also accepted. |
| `OPENAI_API_KEY` | — | For voice message transcription (Whisper) |
| `SCHEDULER_INTERVAL` | — | Scheduler check interval in seconds (default: `60`; accepted range: `1`–`86400`) |
| `UPDATE_CHECK_INTERVAL` | — | Auto-update check interval in seconds (default: `3600`; accepted range: `1`–`2592000`) |
| `REMINDER_MAX_AGE_SECONDS` | — | Reminders whose `fire_at` is older than this limit are marked `failed` with `failure_reason="expired"` instead of retrying forever (default: `604800` = 7 d; accepted range: `1`–`31536000`). |
| `CLAIM_TIMEOUT_SECONDS` | — | Stale-claim reset timeout for reminders and scheduled tasks (default: `600` = 10 min). A slower delivery watcher now has more room before the scheduler decides the claim is stuck. |
| `SMOKE_TEST_TIMEOUT_SECONDS` | — | Post-update smoke-test timeout (default: `60`). Raise on slow machines / cold caches to avoid false-positive rollbacks. |
| `VOICE_TIMEOUT_SECONDS` | — | Voice (Whisper) transcription HTTP timeout (default: `60`). |
| `AI_IDLE_TIMEOUT` | — | Max seconds the AI subprocess may stay silent (no stream-json output) before Robyx considers it hung and kills it (default: `600` = 10 min). A responsive agent that keeps emitting lines stays alive indefinitely up to `AI_TIMEOUT`. Only applies to the streaming path (Claude Code, OpenCode). |
| `AI_TIMEOUT` | — | Hard wall-clock cap per AI invocation in seconds (default: `7200` = 2 h). Safety net for runaway processes and the sole timeout on the non-streaming path. On streaming backends, prefer tuning `AI_IDLE_TIMEOUT` — this is rarely hit when the agent is actually producing output. |

Existing installations need no `.env` migration for collaborative security:
an absent or invalid `COLLAB_PARTICIPANT_POLICY` resolves fail-closed to
`disabled`; there is deliberately no `full` value. Set `read-only` explicitly
only for workspaces whose readable contents may be disclosed to participants.

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

## Safe updates from chat

You can update settings in chat with explicit assignments like
`OPENAI_API_KEY=sk-...` or `AI_BACKEND=codex`. The chat-editable allow-list is:
`OPENAI_API_KEY`, `AI_BACKEND`, `AI_CLI_PATH`, `CLAUDE_PERMISSION_MODE`,
`SCHEDULER_INTERVAL`, `UPDATE_CHECK_INTERVAL`, `REMINDER_MAX_AGE_SECONDS`,
`ROBYX_PLATFORM`, `ROBYX_WORKSPACE` and the two legacy
`KAELOPS_PLATFORM` / `KAELOPS_WORKSPACE` aliases.

Robyx validates every value against the same typed schema used by setup and
runtime. An invalid enum, integer range, executable path or workspace path is
rejected locally and is never sent to the AI backend. Replies identify the key
and expected shape but never echo the submitted value. Optional values that
support clearing can be set to an explicitly quoted empty value, for example
`OPENAI_API_KEY=""`.

For an accepted update Robyx writes `.env` through an atomic `0600` file
replacement, then starts a fresh isolated Python process that imports the
candidate runtime configuration. Only after that check succeeds does it
announce the update and restart. If the check fails, the previous `.env`
contents and mode are restored before the operator is notified; no restart is
attempted.

`COLLAB_PARTICIPANT_POLICY` is excluded from that chat surface. Change it only
in the server's `.env`, then restart Robyx.

## Platform Migration

Switch between Telegram, Discord, and Slack at any time. Just tell Robyx:

> "Passa a Discord" / "Switch to Slack" / "Migrate to Telegram"

Robyx will guide you through the target platform's required credentials and IDs, update the configuration, and restart. Telegram needs a bot token plus chat and owner IDs; Slack needs bot and app tokens plus control-room and owner IDs; Discord needs a bot token plus guild, control-room, and owner IDs. Agent briefs and memory remain on disk, but platform channel IDs and persisted task ownership scopes are not portable: recreate or explicitly rebind workspaces and scheduled destinations on the target platform before resuming them.

---

← [Back to README](../README.md)
