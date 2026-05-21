# Feature Specification: Discord Parity — Collaborative Workspaces, leave_chat, Invite Link

**Feature Branch**: `007-discord-parity`
**Created**: 2026-05-13
**Status**: Draft
**Input**: User description: "Replicate the Telegram collaborative-workspace flow on Discord — bot-added/bot-removed/bot-migrated lifecycle events, authorization (audit-log lookup + manual escape hatch), `leave_chat`, invite-link generation, platform-agnostic mention parsing — and lift the `not yet supported` guard introduced in spec 003 (FR-013). Documents irreducible API differences as limits, not workarounds."

## Context

Spec [003-external-group-wiring](../003-external-group-wiring/spec.md) wired the Telegram bot-added/bot-removed/bot-migrated lifecycle into the collaborative-workspace state machine and explicitly deferred Discord and Slack with a `justified violation` of Constitution Principle I (Multi-Platform Parity). The "not yet supported" guard lives in `bot/bot.py` for both platforms and surfaces a single-message refusal when the bot is added to a Discord guild or Slack channel.

Spec 007 **closes the justified violation of Principle I declared in specs/003-external-group-wiring/spec.md#FR-013** for Discord. Slack closure is tracked separately in spec 008. The platform documentation sweep is tracked in spec 009.

**Scope boundaries**:
- This spec is about **Discord** lifecycle for collaborative workspaces and the two adapter capabilities it requires (`leave_chat`, `get_invite_link`). It introduces the platform-agnostic abstractions (`ChatRef`, `LifecycleAdded/Removed/Migrated`) that spec 008 will reuse without modification.
- Spec 008 (Slack) is **out of scope**. The Slack adapter remains in its current "unsupported notice" state until that spec lands.
- Spec 009 (docs sweep) is **out of scope**. This spec only touches `docs/team.md`, `docs/configuration.md`, and `docs/architecture.md` to the extent required for the new Discord flow.
- Spec 006 dedicated-topic operations on Discord are **already implemented** (best-effort) and unchanged.

## Clarifications

### Session 2026-05-13

- Q: What is the canonical form of a Discord `chat_id` in `CollabWorkspace.chat_id`? → A: `"<guild_id>:<channel_id>"` (string, colon-separated). Telegram stores `"<chat_id>"` (numeric serialized to string); Slack will store `"<team_id>:<channel_id>"` (spec 008). One guild MAY host multiple collaborative workspaces, each bound to a distinct channel. Granularity is **per-channel, not per-guild**.
- Q: How does Discord identify "who added the bot" when `on_guild_join` does not carry the inviter? → A: Audit-log lookup against `AuditLogAction.bot_add` with up to 3 retries (1s/2s/4s backoff) and the `View Audit Log` permission. If the lookup fails (forbidden, empty result, sustained API failure), the system falls back to a manual claim command (`/im-the-owner <workspace-name>`) posted by the user in any channel of the guild. Fail-closed default: `added_by_id=None`, refusal until the user claims.
- Q: How does Flow A (deep-link pre-announce) work on Discord, given that Discord OAuth `state` is not visible to the bot? → A: Same as the unauthorized-adder guard — audit-log match on `bot_add` action. The pre-announced workspace carries `expected_creator_id` (existing field) AND a new `expected_platform` field. If the audit-log inviter id matches `expected_creator_id` AND `expected_platform` matches the guild's platform, Flow A binds the pending workspace. Otherwise the manual `/im-the-owner` claim is the escape hatch.
- Q: When `leave_chat` is invoked on a Discord guild that hosts more than one active collaborative workspace, does the bot leave the entire guild? → A: No. The handler consults `CollabStore.find_active_in_guild(guild_id)` BEFORE calling `platform.leave_chat`. If other active workspaces remain, the bot does **not** leave the guild — only the offending workspace is closed (existing `collab_bot_removed` semantics) and a single log line `collab.leave_chat skipped: guild has N other active workspaces` is emitted. The adapter `leave_chat` method stays "dumb" — policy lives in the handler, not in the adapter.
- Q: What invite-link parameters are exposed? → A: TTL and max-uses as env vars (`DISCORD_INVITE_TTL_DAYS=7`, `DISCORD_INVITE_MAX_USES=10`), with no per-workspace override in MVP. Generated via `channel.create_invite(max_age=..., max_uses=..., reason="Robyx collaborative workspace invite")`.
- Q: How are cross-platform user id collisions prevented (Telegram user `123` vs Discord user `123`)? → A: New `CollabWorkspace.expected_platform` field. When `[COLLAB_ANNOUNCE]` runs in HQ, the platform of the orchestrator is recorded into the pending workspace; lifecycle dispatch refuses to bind a pending workspace if its `expected_platform` does not match the platform of the bot-added event.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Discord-native collaborative workspace via Flow A deep-link (Priority: P1)

The owner, working in HQ on Telegram, instructs the orchestrator to spin up a collaborative workspace and explicitly asks for it to live on Discord. The orchestrator emits `[COLLAB_ANNOUNCE name="atlas" display="Atlas Project" purpose="..." platform="discord"]`. A pending `CollabWorkspace` is persisted with `platform="discord"`, `chat_id="0"`, `expected_creator_id=<the owner's Discord user id>`, `expected_platform="discord"`. The owner opens the Discord OAuth invite URL, picks a guild they own, and adds Robyx to a specific channel (or default). On `on_guild_join`, the bot looks up the audit log, finds the owner as the inviter, matches the pending workspace, binds `chat_id="<guild_id>:<channel_id>"` (the first text channel where the bot can post — or the channel explicitly chosen if discord supplies one in the event), generates an invite link, posts the welcome message in that channel, and notifies HQ on Telegram that the workspace is configured.

**Why this priority**: Flow A is the headline new capability — pre-announcing a workspace from HQ and having it materialize on a different platform. Without it, multi-platform parity is just "rename channels"; with it, Robyx becomes a true cross-platform orchestrator.

**Independent Test**: From a Telegram HQ session, emit `[COLLAB_ANNOUNCE name="atlas-007" display="Atlas Test" purpose="testing 007" platform="discord"]`. Verify `data/collaborative_workspaces.json` carries a pending record with `platform="discord"`, `chat_id="0"`, `expected_platform="discord"`. From a Discord client, add Robyx to a guild. Verify (a) audit-log lookup succeeds and pulls the owner's id, (b) pending record matches, (c) record is promoted to `active` with `chat_id="<guild>:<channel>"`, (d) Discord channel receives `collab_welcome_pending` message, (e) Telegram HQ receives `collab_bot_added_hq_matched` notification with the Discord invite link, (f) the agent file `data/agents/atlas-007.md` is unchanged.

**Acceptance Scenarios**:

1. **Given** a pending workspace `atlas-007` with `platform="discord"`, `expected_creator_id=<owner-discord-id>`, **When** the owner adds Robyx to a Discord guild, **Then** the bot looks up the audit log, finds the owner as `bot_add` inviter within 3 retries, binds the workspace to `chat_id="<guild>:<channel>"` and `status="active"`.
2. **Given** the same pending workspace, **When** an unauthorized user (not the owner, not bot-owner, not OWNER/OPERATOR in any active workspace) adds Robyx to a different Discord guild, **Then** the bot refuses, sends `collab_unauthorised_adder` to the channel, calls `platform.leave_chat` for the guild, and notifies HQ via `collab_unauthorised_adder_hq` — the pending workspace remains pending.
3. **Given** a pending workspace with `expected_platform="discord"`, **When** the owner's Telegram account adds Robyx to a Telegram group, **Then** the pending workspace is **not** bound (cross-platform refusal). The Telegram add follows Flow B (in-group setup) instead.
4. **Given** the pending workspace's invite link is generated, **When** the welcome message is posted in the Discord channel, **Then** the Discord channel receives a default invite with TTL 7 days and 10 uses, and `CollabWorkspace.invite_link` stores the resulting URL.

---

### User Story 2 — Audit log failure + manual `/im-the-owner` escape hatch (Priority: P1)

The bot is added to a Discord guild but the audit-log lookup fails — either the bot lacks the `View Audit Log` permission (`discord.Forbidden`), the audit log is empty (race condition: lookup ran before Discord wrote the entry), or all 3 retries time out. The bot does NOT silently fail and does NOT silently bind the workspace. Instead, it posts a message in the guild's first writable channel explaining that it could not determine who added it, and instructs the user to type `/im-the-owner <workspace-name>` in any channel of the guild to manually claim a pending workspace. The user types the command; the bot validates that (a) a pending workspace with `expected_platform="discord"` and `expected_creator_id=<user's Discord id>` exists, (b) the workspace's `platform` is `"discord"`, (c) the workspace is still in `status="pending"`. On success, the workspace binds to the channel where the command was issued.

**Why this priority**: The audit-log path is fragile by design (Discord's choice — `on_guild_join` does not carry the inviter). Without an explicit fallback, every permission misconfiguration would silently break collaborative workspaces on Discord. The `/im-the-owner` command is the user-visible recovery path.

**Independent Test**: Disable the `View Audit Log` permission for the Robyx bot in a Discord guild. From Telegram HQ, pre-announce a Discord workspace. Add Robyx to the guild. Verify: (a) audit log lookup raises `Forbidden`, (b) the bot posts a fallback explanation in the first writable channel, (c) the pending workspace stays in `status="pending"`, (d) issuing `/im-the-owner atlas-007` in any channel of the guild binds the workspace and triggers the same downstream flow as the audit-log path (welcome + HQ notification + invite link).

**Acceptance Scenarios**:

1. **Given** the bot lacks `View Audit Log`, **When** Robyx joins a Discord guild, **Then** `audit_logs(action=bot_add, limit=5)` raises `discord.Forbidden`, the handler posts `STRINGS["discord_audit_log_unavailable"]` in the first writable channel, and the pending workspace remains pending.
2. **Given** the bot has the permission but the audit log returns empty across all 3 retries, **When** Robyx joins, **Then** the same fallback message is posted (`STRINGS["discord_audit_log_empty"]`); the pending workspace remains pending.
3. **Given** the fallback message has been posted and a pending workspace with `expected_creator_id=<user-id>` and `expected_platform="discord"` exists, **When** that user issues `/im-the-owner atlas-007` in any channel of the guild, **Then** the workspace binds to `chat_id="<guild>:<channel>"`, `status="active"`, welcome + invite + HQ notification fire identically to the audit-log path.
4. **Given** the same setup, **When** a different user (not the pending workspace's `expected_creator_id`) issues `/im-the-owner atlas-007`, **Then** the command is refused (`STRINGS["im_the_owner_creator_mismatch"]`) and the workspace stays pending.
5. **Given** the same setup, **When** the user issues `/im-the-owner nonexistent`, **Then** the command is refused (`STRINGS["im_the_owner_unknown_workspace"]`) and no state changes.

---

### User Story 3 — Flow B (ad-hoc add on Discord) (Priority: P2)

A user (the bot owner, or a holder of OWNER/OPERATOR in an active workspace) adds Robyx to a Discord guild without first pre-announcing a workspace from HQ. The audit-log lookup succeeds and identifies the user as authorized (`is_authorised_adder` returns True). The bot creates a provisional `CollabWorkspace` with `platform="discord"`, `status="setup"`, `chat_id="<guild>:<channel>"` (the channel inferred by the same rules as US1), writes the seed `data/agents/<name>.md` file, registers the agent, generates an invite link, posts a Flow-B bootstrap prompt in the Discord channel, and notifies HQ that an ad-hoc setup is in progress. The same AI-driven `[COLLAB_SETUP_COMPLETE]` macro that closes the loop on Telegram closes the loop on Discord.

**Why this priority**: Flow B is the legacy add-and-configure path. P2 because most production use will be Flow A; Flow B is the resilience fallback.

**Independent Test**: From Discord, add Robyx to a guild as the bot owner without pre-announcing. Verify: (a) audit-log lookup succeeds with bot-owner id, (b) `is_authorised_adder` returns True, (c) a provisional workspace `collab-<title-slug>` is created with `platform="discord"`, `status="setup"`, (d) `data/agents/<name>.md` is written, (e) Flow-B bootstrap message posts in the Discord channel, (f) HQ on Telegram receives `collab_bot_added_hq_pending`. When the setup agent emits `[COLLAB_SETUP_COMPLETE purpose="..." inherit_memory="true"]`, the workspace promotes to `active` and HQ receives `collab_setup_complete_hq`.

**Acceptance Scenarios**:

1. **Given** the bot owner adds Robyx to a Discord guild with no pending workspace, **When** the audit log resolves the bot owner as inviter, **Then** a provisional workspace is created with `platform="discord"`, `status="setup"`, and the Flow-B bootstrap prompt is posted in the inferred channel.
2. **Given** an active-workspace OPERATOR adds Robyx to a different Discord guild, **When** `is_authorised_adder` returns True, **Then** Flow B proceeds identically to US3.1.
3. **Given** an unauthorized user adds Robyx, **When** the audit-log lookup succeeds and resolves the unauthorized user, **Then** the same refusal flow as US1.2 fires (message in chat + leave_chat policy + HQ notification).

---

### User Story 4 — Bot removed / kicked from Discord guild (Priority: P2)

A user removes Robyx from a Discord guild (kick, ban, or `guild.leave()` initiated by the user). `on_guild_remove` fires. The lifecycle handler resolves the `CollabWorkspace` bound to `chat_id="<guild>:<channel>"`, closes it (`status="closed"`), and notifies HQ. If the guild hosts multiple active workspaces (mapped to different channels), only the workspace whose `chat_id` matched the removed guild's channel(s) is closed — see also US5 leave_chat policy.

**Why this priority**: Mirror of Flow A/B. Without bot-removed wiring, closed Discord guilds leave stale workspaces in the routing store.

**Independent Test**: With an active Discord workspace, kick Robyx from the guild. Verify (a) `on_guild_remove` fires, (b) `CollabStore.close(ws.id)` succeeds, (c) HQ receives `collab_bot_removed_hq` with the workspace display name, (d) the workspace's `status="closed"`, (e) `find_active_in_guild(guild_id)` no longer returns the workspace.

**Acceptance Scenarios**:

1. **Given** an active Discord workspace, **When** Robyx is removed from the guild, **Then** `on_guild_remove` triggers `collab_bot_removed` with a `LifecycleRemoved` event carrying `chat_ref=ChatRef("discord", "<guild>:<channel>")` and the workspace is closed.
2. **Given** the same guild hosts two active workspaces in two different channels, **When** Robyx is removed from the guild, **Then** both workspaces are closed (one `on_guild_remove` event triggers cleanup for every workspace whose `chat_id` starts with the guild prefix).

---

### User Story 5 — `leave_chat` policy for shared guilds (Priority: P2)

When the unauthorized-adder guard fires on a Discord guild that hosts another active workspace, the bot must NOT leave the entire guild — that would orphan the legitimate workspace. The policy lives in `bot/handlers.py` (not the adapter): before calling `platform.leave_chat(chat_ref)`, the handler calls `CollabStore.find_active_in_guild(guild_id)`. If any other active workspace shares the guild, the handler skips `leave_chat`, posts the refusal message in the offending channel only, and logs `collab.leave_chat skipped: guild has N other active workspaces`. The bot stays in the guild for the legitimate workspace; the offending channel sees the refusal and no further action is taken there.

**Why this priority**: Without this policy, the FR-011 refusal flow (spec 003) destroys legitimate cross-workspace state every time an unauthorized user adds the bot to a shared guild.

**Independent Test**: With workspace A active in `<guild>:<channel-1>`, simulate an unauthorized add to `<guild>:<channel-2>`. Verify (a) refusal message in `<channel-2>`, (b) `platform.leave_chat` is NOT called, (c) workspace A is unchanged (`status="active"`, `chat_id="<guild>:<channel-1>"`), (d) the bot remains a member of the guild.

**Acceptance Scenarios**:

1. **Given** workspace A active in `<guild>:<chan-1>` and an unauthorized add to `<guild>:<chan-2>`, **When** the guard fires, **Then** `find_active_in_guild(<guild>)` returns `[A]`, `platform.leave_chat` is skipped, the refusal message is posted only in `<chan-2>`, and A is unchanged.
2. **Given** workspace A active in `<guild>:<chan-1>` and an unauthorized add to a **different** guild, **When** the guard fires, **Then** `find_active_in_guild(<other-guild>)` returns `[]`, `platform.leave_chat(<other-guild>)` is called, and the bot leaves the other guild. A is unchanged.

---

### User Story 6 — Cross-platform mention parsing for role commands (Priority: P3)

`/promote @user`, `/demote @user`, and similar commands in a Discord workspace must accept Discord mention syntax (`<@123>`, `<@!123>`) in addition to the existing numeric-id and `@username` forms. The role storage (`CollabWorkspace.roles: dict[str, str]`) already keys by `str(user_id)` and accepts both int and str user ids, so the only change is parser-level.

**Why this priority**: Role management on Discord is unusable without mention parsing. P3 because basic provisioning (US1–US5) works without it; users can paste numeric ids as a workaround.

**Independent Test**: In a Discord collaborative workspace, run `/promote <@456789012345678901>`. Verify (a) `_parse_user_id` returns `456789012345678901` (int), (b) `collab_ws.set_role(456789012345678901, Role.OPERATOR)` is invoked, (c) `roles` dict stores `"456789012345678901": "operator"`. Run `/promote @123` (numeric @-prefix form). Verify `_parse_user_id` returns `123`.

**Acceptance Scenarios**:

1. **Given** a Discord collaborative workspace, **When** `/promote <@456789012345678901>` is issued, **Then** the user `456789012345678901` is promoted to OPERATOR.
2. **Given** the same workspace, **When** `/promote <@!456789012345678901>` is issued (nickname-aware form), **Then** the same result.
3. **Given** the same workspace, **When** `/promote @123` is issued (legacy numeric form), **Then** user `123` is promoted.
4. **Given** the same workspace, **When** `/promote garbage` is issued, **Then** `_parse_user_id` returns None and the command surfaces a user-visible "couldn't parse user id" error.

---

### Edge Cases

- **Discord guild has zero writable channels for the bot**: `on_guild_join` finds no channel where `permissions_for(guild.me).send_messages` is True. The handler logs a warning, leaves the guild, and notifies HQ (FR-DISC-AUDIT-FALLBACK). No pending workspace is bound.
- **Audit log race condition**: Discord may not have written the audit log entry by the time `on_guild_join` fires. The 3-retry exponential backoff (1s/2s/4s) covers this; if all retries return empty, the fallback `/im-the-owner` path is offered.
- **Two pending workspaces match the same Discord inviter**: An unlikely but possible state (the owner pre-announced two Discord workspaces). The matcher picks the most recent (`max(created_at)`) — same behavior as the existing Telegram Flow A.
- **Guild deletes the channel Robyx is bound to**: Detected at next message-send when `discord.NotFound` surfaces. Spec 006's `TopicUnreachable` handling already covers this; collaborative workspace closure on channel-delete is out of scope for 007 (tracked separately).
- **`/im-the-owner` issued in a channel where the bot lacks send_messages**: The command silently fails (no message can be sent back); the user discovers the failure by absence of confirmation. Acceptable for MVP — users have to grant write access for any meaningful use of the bot anyway.
- **`leave_chat` called on a guild the bot is not in**: `discord.client.get_guild()` returns None; `fetch_guild()` raises `discord.NotFound`. The adapter logs WARN and returns without error.
- **Legacy state migration**: A workspace created on Telegram pre-0.28 has `chat_id: int` in the JSON. The migration `v0_28_0.py` converts it to `chat_id: "<int>"` and sets `platform="telegram"`. The `from_dict` loader tolerates both shapes in-process.
- **Race between migration and bot startup**: The migration runs before the scheduler starts (existing migration runner ordering). Boot is blocked on migration completion (idempotent).

## Requirements *(mandatory)*

### Functional Requirements

#### Lifecycle abstraction (foundational; reused by spec 008)

- **FR-001**: The system MUST introduce a platform-agnostic dataclass `ChatRef(platform: str, chat_id: str)` in `bot/messaging/base.py`. Canonical encodings: Telegram = `"<chat_id_int>"`; Discord = `"<guild_id>:<channel_id>"`; Slack (reserved for spec 008) = `"<team_id>:<channel_id>"`. The dataclass MUST be frozen, hashable, and JSON-serialisable via a plain `{"platform": ..., "chat_id": ...}` representation.
- **FR-002**: The system MUST introduce three lifecycle event dataclasses in `bot/messaging/base.py`: `LifecycleAdded(chat_ref, chat_title, added_by_id, added_by_name, raw_event)`, `LifecycleRemoved(chat_ref, chat_title, raw_event)`, `LifecycleMigrated(old_chat_ref, new_chat_ref, raw_event)`. `added_by_id` is `int | str | None` (int for Telegram/Discord, str reserved for Slack, None when unknown).
- **FR-003**: The `Platform` ABC in `bot/messaging/base.py` MUST expose three optional Awaitable callback attributes — `on_added`, `on_removed`, `on_migrated` — each typed as `Callable[[LifecycleAdded|Removed|Migrated], Awaitable[None]] | None`. Adapters dispatch their native lifecycle events through these callbacks. `bot.py` registers the handlers once, regardless of platform, and never branches on `platform.__class__.__name__`.
- **FR-004**: The `Platform` ABC MUST expose a `bot_user_id` property (`int | str | None`) so adapters can filter `member_joined_channel` (Slack) and equivalent events for "is this me?" tests without each adapter reinventing the lookup.

#### Schema and storage

- **FR-005**: The `CollabWorkspace` dataclass in `bot/collaborative.py` MUST gain three new fields: `platform: str = "telegram"` (back-compat default), `chat_id: str = "0"` (changed from `int` to `str`; `"0"` denotes pending-unbound), `expected_platform: str | None = None`. `from_dict` MUST accept legacy `chat_id: int` records and coerce to `str(chat_id)` on load. `to_dict` MUST always emit `chat_id: str`.
- **FR-006**: `CollabStore._chat_map` MUST become `dict[tuple[str, str], str]` keyed by `(platform, chat_id)`. The API MUST expose:
  - `get_by_chat_id(chat_ref: ChatRef) -> CollabWorkspace | None`
  - `update_chat_id(ws_id: str, chat_ref: ChatRef, *, expected_creator_id: int | str | None = None) -> bool`
  - `migrate_chat_id(old_ref: ChatRef, new_ref: ChatRef) -> bool`
  - `find_active_in_guild(guild_id: str) -> list[CollabWorkspace]` (Discord-specific helper; returns active workspaces whose `platform=="discord"` and `chat_id` starts with `f"{guild_id}:"`)
  - `find_active_by_platform(platform: str) -> list[CollabWorkspace]` (forward-compat, used by spec 008)
- **FR-007**: A new migration `bot/migrations/v0_28_0.py` MUST run idempotently before the scheduler boots. For each record in `data/collaborative_workspaces.json`: set `platform="telegram"` if missing; coerce `chat_id` to `str` if int. The migration MUST be safe to run twice (second run is a no-op). Atomic write via the existing `temp-file + os.replace` pattern. Snapshot+rollback handled by the existing migration runner.

#### Discord lifecycle plumbing

- **FR-008**: The Discord adapter (`bot/messaging/discord.py`) MUST implement `bot_user_id` (returns `self._client.user.id` post-`on_ready`), `leave_chat(chat_id)` (parses `"<guild>:<channel>"`, calls `guild.leave()` after the handler-level policy gate), and `get_invite_link(chat_id)` (calls `channel.create_invite(max_age=DISCORD_INVITE_TTL_DAYS*86400, max_uses=DISCORD_INVITE_MAX_USES, reason="Robyx collaborative workspace invite")`).
- **FR-009**: The Discord adapter MUST register `on_guild_join` and `on_guild_remove` callbacks that translate Discord's native events into `LifecycleAdded` / `LifecycleRemoved` and invoke `self.on_added` / `self.on_removed`. The adapter MUST perform the audit-log lookup (`guild.audit_logs(action=AuditLogAction.bot_add, limit=5)`) with 3 retries (1s/2s/4s); on success the resolved inviter id populates `LifecycleAdded.added_by_id`; on failure (Forbidden, empty, or sustained API error), `added_by_id=None` and the handler is responsible for the `/im-the-owner` fallback.
- **FR-010**: The `on_guild_join` handler MUST pick the first text channel where `permissions_for(guild.me).send_messages` is True as the `chat_id` for the new workspace. If no writable channel exists, the bot logs a warning, calls `guild.leave()`, and notifies HQ — no pending workspace binds.
- **FR-011**: `bot/handlers.py` `collab_bot_added`, `collab_bot_removed`, `collab_bot_migrated` MUST change signatures to accept `LifecycleAdded`/`Removed`/`Migrated` events (carrying `chat_ref`). All branching on platform MUST go through `event.chat_ref.platform` — never `if isinstance(platform, DiscordPlatform)`. `bot/bot.py` MUST register these handlers as callbacks on `platform.on_added/removed/migrated` for **every** adapter that supports lifecycle, with Telegram, Discord, and Slack registering identical wiring (Slack lifecycle remains a stub until spec 008).
- **FR-012**: The "not yet supported on Discord" guard in `bot/bot.py:on_guild_join` MUST be removed. The `STRINGS["collab_unsupported_platform_discord"]` key MUST be removed from `bot/i18n.py`.

#### Authorization

- **FR-013**: When a pending `CollabWorkspace` has `expected_platform != None`, lifecycle dispatch MUST refuse to bind that workspace unless `event.chat_ref.platform == expected_platform`. The refusal MUST log `collab.match.platform_mismatch ws_id=... expected=... got=...` and leave the workspace pending.
- **FR-014**: `is_authorised_adder` in `bot/authorization.py` MUST accept `user_id: int | str | None` so a future Slack user id (`"U..."` string) does not require a second signature change. The body already operates on `dict[str, str]` for role lookups, so the change is type-only.
- **FR-015**: A new chat command `/im-the-owner <workspace-name>` MUST be implemented in `bot/handlers.py`. Available on Discord (and reserved for Slack in spec 008; Telegram does not need it because audit log is not required there). Pre-conditions: the issuing user MUST have a pending workspace where `expected_creator_id == user_id` AND `expected_platform == "discord"` AND `name == workspace-name`. On success: same downstream flow as the audit-log path (welcome + invite + HQ notify). On failure: deterministic error strings (`im_the_owner_unknown_workspace`, `im_the_owner_creator_mismatch`, `im_the_owner_platform_mismatch`, `im_the_owner_already_bound`).

#### `leave_chat` and invite-link policies

- **FR-016**: In `bot/handlers.py` `collab_bot_added`, the unauthorized-adder guard MUST call `CollabStore.find_active_in_guild(guild_id)` before invoking `platform.leave_chat(chat_ref)` on Discord. If any other active workspace shares the guild, the handler MUST skip `leave_chat`, log `collab.leave_chat.skipped guild=... active_count=...`, and post the refusal only in the offending channel. The same logic applies to Slack in spec 008.
- **FR-017**: Discord invite-link generation MUST use the config env vars `DISCORD_INVITE_TTL_DAYS` (default 7) and `DISCORD_INVITE_MAX_USES` (default 10), both readable via `bot/config.py`. Invalid values (negative, non-int) MUST fall back to defaults with a warning log.

#### Mention parsing

- **FR-018**: `bot/handlers.py` `_parse_user_id(text: str)` MUST accept all of these forms:
  - `<@123456789>` and `<@!123456789>` (Discord) → return `int`
  - `<@U12345>` (Slack — reserved for spec 008; accept and return `str` now to avoid a second parser refactor) → return `str`
  - `@username`, `@123`, `123` (Telegram legacy) → return `int` when numeric, `None` when alpha-only
- **FR-019**: `CollabWorkspace.set_role`, `get_role`, `remove_user`, `is_owner`, `can_execute` MUST accept `user_id: int | str`. Internal storage is already `dict[str, str]`; only the type hints and a stringification in callers change.

#### Documentation (scoped to 007 only — see spec 009 for full sweep)

- **FR-020**: `docs/team.md` MUST gain a "Collaborative workspaces — Discord" subsection covering: Flow A pre-announce with `platform="discord"`, Flow B ad-hoc add, audit-log permission requirement, `/im-the-owner` fallback, invite-link defaults, shared-guild `leave_chat` policy.
- **FR-021**: `docs/configuration.md` MUST document the new env vars `DISCORD_INVITE_TTL_DAYS` and `DISCORD_INVITE_MAX_USES`, plus the Discord bot permissions required (`View Audit Log`, `Create Instant Invite`, plus existing `Send Messages`, `Manage Channels`).
- **FR-022**: `docs/architecture.md` MUST gain a short diagram or block describing the lifecycle-event abstraction (adapter → `LifecycleAdded` → handler dispatch); used by spec 009 as the seed for the broader cross-platform docs.

### Key Entities *(include if feature involves data)*

- **CollabWorkspace** (extended): same as today, plus `platform: str`, `chat_id: str` (was `int`), `expected_platform: str | None`.
- **ChatRef** (new): platform-agnostic chat identifier `(platform: str, chat_id: str)`; replaces raw `chat_id: Any` in all lifecycle signatures.
- **LifecycleAdded / LifecycleRemoved / LifecycleMigrated** (new): platform-agnostic lifecycle event dataclasses; carry `chat_ref`, optional `chat_title`, optional `added_by_id`/`added_by_name`, and an opaque `raw_event` field for diagnostic logging.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Closing FR-013 of spec 003: the "not yet supported on Discord" guard in `bot/bot.py` is removed; `STRINGS["collab_unsupported_platform_discord"]` is deleted from `bot/i18n.py`; no test asserts the unsupported-platform message on Discord.
- **SC-002**: Adding Robyx to a Discord guild — for a user who pre-announced a Discord workspace from Telegram HQ — succeeds end-to-end (Flow A) within 10 seconds of `on_guild_join`, in 100% of runs where audit log is accessible and the inviter matches `expected_creator_id`.
- **SC-003**: When audit-log lookup fails (Forbidden, empty, sustained errors), the bot posts the fallback message within 10s of `on_guild_join` AND a subsequent `/im-the-owner <name>` from the legitimate user binds the workspace within 5s.
- **SC-004**: Unauthorized adds to a shared Discord guild (where another active workspace lives) trigger refusal in the offending channel only and **never** call `platform.leave_chat`. Validated across 5 simulated scenarios with varied authorization states.
- **SC-005**: The migration `v0_28_0` runs in <1s on a typical install with ≤50 workspaces, sets `platform="telegram"` and `chat_id=str(...)` on every legacy record, is fully idempotent (re-running produces no diff), and leaves no orphan keys in `_chat_map`.
- **SC-006**: All existing Telegram regression tests for spec 003 (test_collaborative.py, test_collab_handlers.py, test_collab_lifecycle.py, test_collab_multiplatform.py) pass after the signature refactor with no behavior change for Telegram. Verified by running `pytest tests/test_collab*` post-Phase-3 of the implementation.
- **SC-007**: `bot/handlers.py` has zero `isinstance(platform, DiscordPlatform)` or string-equality checks against `"discord"` outside the `_parse_user_id` parser and the `find_active_in_guild` call. Dispatch is uniformly through `event.chat_ref.platform`. Verified by grep audit.
- **SC-008**: `_parse_user_id` accepts all 5 documented input shapes (Discord `<@id>`, `<@!id>`, Telegram `@username`, `@id`, bare `id`) and a Slack-style `<@U...>` stub round-trips to a string identifier. Validated by 6 unit tests.
- **SC-009**: Telegram-only workflows are unchanged. A pre-007 backup of `data/collaborative_workspaces.json` round-trips through `v0_28_0` migration + reload + `to_dict` + write to a byte-for-byte equivalent record set modulo the new `platform`/`expected_platform`/`chat_id` type fields.

## Assumptions

- discord.py >= 2.0 is the runtime version (already pinned in the project's dependencies). `AuditLogAction.bot_add`, `Guild.audit_logs`, `channel.create_invite`, `Guild.leave` all behave per the public discord.py docs.
- Discord guild + channel ids fit in `uint64`. `chat_id="<guild>:<channel>"` strings are always parseable by `chat_id.split(":", 1)` returning two integer-coercible strings.
- The bot owner's Discord user id is configured separately from the Telegram owner id (existing `OWNER_ID` env var refers to the Telegram id by convention; cross-platform owner identity is a documented future concern, out of scope for 007).
- Slack remains in its "unsupported notice" state through spec 007. Spec 008 will reuse `ChatRef`, the lifecycle dataclasses, the schema fields, `find_active_by_platform`, and `_parse_user_id` Slack mention support without modification.
- `data/collaborative_workspaces.json` is the only on-disk artifact the migration touches. Agent files (`data/agents/<name>.md`) and `data/agent_state.json` are unaffected.
- No release artifacts (VERSION, CHANGELOG, releases/) are produced by spec 007 individually — they land at the end of all phases (see plan.md Phase 9).

## Out of Scope

- Slack lifecycle and `member_joined_channel` wiring — tracked in spec 008.
- Documentation sweep across all `docs/*.md` files — tracked in spec 009. This spec touches only the three files needed to operate the Discord flow.
- Dedicated-topic operations on Discord (spec 006 already implemented best-effort there).
- Discord slash-command framework (interactions API). `/im-the-owner` is implemented as a text command parsed by the existing `bot/handlers.py:on_message` path on Discord — the same approach as Telegram's `/help`, `/status`, etc.
- Cross-platform user identity (linking the owner's Telegram and Discord ids). The new `expected_platform` field is a refusal gate, not a linkage table.
- Migration of dedicated continuous-task topics across platforms.

## Constitution Alignment

This spec **closes the justified violation of Principle I declared in specs/003-external-group-wiring/spec.md#FR-013** for Discord. Slack closure follows in spec 008; full Principle-I parity will be re-asserted in spec 009's release notes.

- **I. Multi-Platform Parity**: spec 007 lifts the Discord exception. Telegram and Discord reach functional parity for collaborative workspaces. Slack remains a documented limitation pending spec 008.
- **II. Chat-First Configuration**: all new user-visible knobs (`[COLLAB_ANNOUNCE platform="discord"]`, `/im-the-owner`) are chat-driven. No file edits required.
- **III. Resilience & State Persistence**: the migration is idempotent and atomic; backwards-compat in `from_dict` tolerates partial rollouts. No external dependencies introduced.
- **IV. Comprehensive Testing**: every new contract (lifecycle abstraction, ChatRef, audit-log helper, `/im-the-owner`, `leave_chat` policy, invite-link, mention parser) ships with golden-path + at least one error/edge test. Telegram regression suite is run as part of every phase.
- **V. Safe Evolution**: migration `v0_28_0` follows the existing pattern (`bot/migrations/v0_28_0.py` with `MIGRATION` constant). Backwards-compat in-process via `from_dict`. Rollback-safe (old code reads new state with the new fields ignored as unknown keys).
