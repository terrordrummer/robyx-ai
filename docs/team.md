# Building Your Team

← [Back to README](../README.md)

This is the core of Robyx: **you build your team through conversation**.

## Start with Robyx

Tell Robyx what you need in natural language:

```
"I need a React app workspace, a Python API workspace, and an infrastructure workspace.
Create one for each."
```

Robyx creates three workspace topics/channels and three agents with appropriate instructions. By default, each new workspace inherits the configured `ROBYX_WORKSPACE` as its starting `work_dir`; Robyx does not auto-map each workspace to a separate project directory.

## Add Specialists

As you work, you'll notice patterns — tasks that cut across projects:

```
"Create a code reviewer specialist that checks for security issues
and follows our team's Python conventions."
```

Now any workspace agent can call `@code-reviewer` when it needs a review.

## Evolve Over Time

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

## Refreshing an Agent — `/clear`

Long-running sessions on Claude/Codex/OpenCode can degrade — the longer the context, the slower and less focused the responses become. The `/clear` command (added in v0.28.0, spec 007.1) gives you a one-step reset:

```
/clear
```

Used inside a workspace topic, a collaborative-workspace chat, or a specialist topic, `/clear`:

1. Archives the conversation since the previous `/clear` (or since the agent was created) to `data/conversations/<agent>/archive-<UTC-timestamp>.md`.
2. Regenerates the agent's AI-CLI session id — the next message starts a fresh session.

From HQ you can also clear a specific agent by name: `/clear my-specialist`. `/clear` issued bare in HQ is **refused** — the orchestrator stays session-continuous on purpose.

### Recovering archived context

When you want the agent to look at a past archive — e.g. "what was I asking you yesterday about Atlas?" — the agent can emit the `[GET_ARCHIVE]` macro:

```
[GET_ARCHIVE since="2d"]                 # last two days, this agent's archives
[GET_ARCHIVE since="6h" name="atlas"]    # last six hours, named agent
[GET_ARCHIVE since="2026-05-01T00:00Z"]  # since an explicit timestamp
```

Robyx intercepts the macro (same lifecycle as `[GET_EVENTS]`), reads the matching markdown files, and injects them back into the same turn as system context. Limit `1..50`, default `10`.

The archive markdown files are also plain on-disk files you can read or grep directly, and they survive across upgrades.

## Why This Approach

Pre-built agent platforms give you 500 skills you didn't ask for and charge you for the complexity. Robyx gives you:

- **Zero skill bloat** — every agent does exactly what you defined
- **Your vocabulary** — agents speak your domain language because you trained them
- **Your workflow** — no adapting to someone else's idea of how work should flow
- **Full transparency** — agent instructions are markdown files you can read and edit
- **No lock-in** — swap AI backends with one env var; everything is files on disk

## Collaborative workspaces — Discord

Since v0.28.0 (spec 007), Robyx can host collaborative workspaces on Discord guilds at functional parity with Telegram. Two flows are supported, mirroring Telegram's behaviour:

**Flow A — pre-announce from HQ.** In your Telegram HQ session, tell the orchestrator to spin up a workspace and say it should live on Discord:

```
"Create a collaborative workspace called atlas for the Atlas project.
It should live on Discord — my Discord user id is 456789012345678901."
```

The orchestrator emits `[COLLAB_ANNOUNCE name="atlas" platform="discord" ...]`. The workspace is persisted as `status="pending"` with `expected_platform="discord"`. When you add Robyx to your Discord guild (via the OAuth bot-invite URL), the bot consults the guild's audit log to confirm you were the inviter, binds the workspace to the first writable channel, generates an invite link, and notifies HQ on Telegram that the workspace is configured.

**Flow B — ad-hoc add.** If you (or any OWNER/OPERATOR of an existing workspace) add Robyx to a Discord guild *without* pre-announcing, the bot creates a provisional workspace and starts a setup conversation in the channel — same pattern as Telegram. The setup agent emits `[COLLAB_SETUP_COMPLETE …]` when configuration is captured.

**Required Discord permissions** (granted via the bot invite URL):

- `View Audit Log` — needed to resolve "who added me" when `on_guild_join` fires. Without it the audit-log lookup fails and the bot falls back to manual claim (see below).
- `Create Instant Invite` — needed to generate the invite URL that Robyx attaches to the HQ notification.
- `Send Messages`, `Manage Channels` — standard message and topic-op permissions.

**`/im-the-owner <workspace-name>` — manual claim.** If the audit-log lookup fails (you didn't grant `View Audit Log`, or Discord's audit log was empty when the bot polled), Robyx posts an advisory in the channel asking you to type:

```
/im-the-owner atlas
```

The bot validates that you match the workspace's `expected_creator_id` and that the workspace is targeted at Discord, then binds it to the channel where you typed the command. Same downstream flow as the audit-log success path.

**Invite-link defaults.** Robyx generates invite URLs with `max_age=DISCORD_INVITE_TTL_DAYS * 86400` and `max_uses=DISCORD_INVITE_MAX_USES`. The defaults are 7 days and 10 uses; both knobs are operator-configurable via env vars (see [Configuration](configuration.md)). A value of `0` is Discord's "no limit" sentinel and is accepted verbatim.

**Shared-guild safety.** A single Discord guild can legitimately host multiple collaborative workspaces — each mapped to a different channel (`chat_id = "<guild>:<channel>"`). When an unauthorized user tries to add Robyx to a channel of a guild where you *already* have a legitimate workspace, the refusal flow is contained: the refusal message lands in the offending channel, but the bot does **not** leave the entire guild (which would orphan the legitimate workspace). The bot stays a member of the guild for the channels you control.

**Slack note.** As of spec 007 Slack is still documented as "not yet supported" for collaborative workspaces. Spec 008 closes that gap using the same `ChatRef` and lifecycle abstractions — the user-visible flow on Slack will be identical to Discord, with `member_joined_channel` standing in for the audit-log lookup (Slack provides the inviter directly).

---

← [Back to README](../README.md)
