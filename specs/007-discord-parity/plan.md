# Implementation Plan: Discord Parity — Collaborative Workspaces, leave_chat, Invite Link

**Branch**: `007-discord-parity` | **Date**: 2026-05-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/007-discord-parity/spec.md`

## Summary

Lift the `not yet supported on Discord` guard introduced by spec 003 (FR-013) and bring the Telegram collaborative-workspace lifecycle (`bot-added` / `bot-removed` / `bot-migrated`) to Discord at functional parity. Introduce a platform-agnostic lifecycle abstraction — `ChatRef`, `LifecycleAdded`, `LifecycleRemoved`, `LifecycleMigrated`, plus `Platform.on_added/on_removed/on_migrated` callable attributes — that spec 008 (Slack) will reuse without modification. Extend `CollabWorkspace.chat_id` from `int` to `str` and add `platform` + `expected_platform` fields; ship a one-time, idempotent migration `v0_28_0`. Solve Discord's `on_guild_join` no-inviter problem with an audit-log lookup (3-retry exponential backoff) plus a `/im-the-owner` escape hatch. Implement `leave_chat` and `get_invite_link` on the Discord adapter; gate `leave_chat` in the handler with a shared-guild policy that consults `CollabStore.find_active_in_guild`. Refactor `_parse_user_id` to accept Discord mention syntax (and reserve Slack mention syntax for 008). All branching in handlers goes through `event.chat_ref.platform` — never adapter `isinstance` checks.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: `python-telegram-bot` (existing), `discord.py >= 2.0` (existing — `AuditLogAction.bot_add`, `Guild.audit_logs`, `Channel.create_invite`, `Guild.leave`), `slack-sdk` (unchanged in 007 — lifecycle stub), stdlib `re`, `json`, `dataclasses`, `pathlib`, `logging`, `asyncio`
**Storage**: JSON files under `data/` (existing pattern preserved). The only on-disk artifact touched by 007 is `data/collaborative_workspaces.json` (schema extended: `platform`, `chat_id: str`, `expected_platform`). No new files. No DB.
**Testing**: pytest; new test modules `tests/test_collab_chat_ref.py`, `tests/test_collab_discord_lifecycle.py`, `tests/test_collab_discord_invite.py`, `tests/test_collab_im_the_owner.py`, `tests/test_migration_v0_28_0.py`; edits to `tests/test_collaborative.py`, `tests/test_collab_handlers.py`, `tests/test_collab_lifecycle.py`, `tests/test_collab_multiplatform.py`. Discord lifecycle tests use `unittest.mock` with `discord.Client` mocks (no live Discord connection).
**Target Platform**: macOS (launchd) + Linux (systemd) — same as today; no new platform.
**Project Type**: Python long-running service (single-project layout). No new top-level dirs.
**Performance Goals**: audit-log lookup completes within 10s (worst-case 1+2+4=7s of retries); migration runs in <1s on ≤50 workspaces; lifecycle dispatch latency unchanged for Telegram (signature change only).
**Constraints**: backwards-compat in-process — `CollabWorkspace.from_dict` accepts both legacy `chat_id: int` and new `chat_id: str` records. Migration strictly idempotent. No hardcoded `if platform == "discord"` outside the well-scoped helpers (`find_active_in_guild`, `_parse_user_id` mention dialect tables).
**Scale/Scope**: ≤50 collaborative workspaces realistic upper bound per install (mirrors existing assumption). One Discord guild MAY host multiple workspaces, each in a distinct channel. `_chat_map` keyspace grows linearly with workspaces, not guilds.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### I. Multi-Platform Parity — **PASS (closes prior justified violation)**

Spec 003 declared a justified violation for Discord and Slack in FR-013. Spec 007 closes that violation for Discord; spec 008 closes it for Slack. The Discord adapter gains `leave_chat` and `get_invite_link`; the lifecycle event abstraction (`Platform.on_added/on_removed/on_migrated`) lets the same handlers serve every platform without branching. Slack remains a documented limitation pending 008 — but the abstraction is platform-agnostic and 008 will be additive.

### II. Chat-First Configuration — **PASS**

`[COLLAB_ANNOUNCE name="X" platform="discord"]` adds a `platform` attribute to the existing chat-only announce macro. `/im-the-owner <name>` is a new chat command, parsed in `bot/handlers.py:on_message` for Discord (same path as `/help`, `/status` on Telegram). No new env vars are required for end-users — only the two optional invite-link knobs (`DISCORD_INVITE_TTL_DAYS`, `DISCORD_INVITE_MAX_USES`) which have sensible defaults.

### III. Resilience & State Persistence — **PASS**

Migration `v0_28_0.py` follows the existing chain pattern (`from_version="0.27.2"` → `to_version="0.28.0"`). Atomic write via `temp-file + os.replace` reusing the existing primitive in `bot/collaborative.py:_write_unlocked`. Idempotency gate: a successful migration leaves every record with `platform` set and `chat_id: str` — re-running is a no-op walk. `from_dict` tolerates both shapes in-process so a partial-rollout state file is never corrupting.

### IV. Comprehensive Testing — **PASS (design-gated)**

Every new public contract has a matching test module enumerated under Project Structure. Telegram regression is run after every phase (`pytest tests/test_collab*`). Discord lifecycle tests use mocks (no live network) per `tests/test_*` convention.

### V. Safe Evolution — **PASS**

Migration is additive (new fields), idempotent, partial-failure resilient (atomic write per-record). Rollback to v0.27.2 would read the extended JSON: `from_dict` in v0.27.2 ignores unknown keys (`platform`, `expected_platform`) and coerces `str` chat_id back via `int(...)` only if the field is wrapped — we verify the in-place rollback safety in T014.

**Result**: All gates pass. Proceed to Phase 0.

### Post-Design Re-Check (after Phase 1 artifacts)

To be re-evaluated after writing `research.md`, `data-model.md`, `contracts/*`, `quickstart.md`. Same five gates; expected re-confirmation of PASS.

## Project Structure

### Documentation (this feature)

```text
specs/007-discord-parity/
├── plan.md              # This file
├── spec.md              # Feature spec (P1–P3 user stories, FR-001..FR-022)
├── research.md          # Phase 0 output — design decisions
├── data-model.md        # Phase 1 output — extended CollabWorkspace + ChatRef
├── quickstart.md        # Phase 1 output — 5 manual Discord scenarios
├── contracts/           # Phase 1 output
│   ├── lifecycle-events.md     # LifecycleAdded/Removed/Migrated dataclass contract
│   ├── chat-ref.md             # ChatRef canonical encodings
│   ├── discord-audit-log.md    # bot_add lookup, retry policy, fallback contract
│   └── im-the-owner.md         # /im-the-owner command grammar + error matrix
├── checklists/
│   └── requirements.md  # Created by /speckit.specify; reviewed at end of plan
└── tasks.md             # Phase 2 output — T001..T051 across 9 phases
```

### Source Code (repository root)

```text
bot/
├── messaging/
│   ├── base.py                # EXTENDED: ChatRef, LifecycleAdded/Removed/Migrated, on_added/on_removed/on_migrated, bot_user_id property
│   ├── telegram.py            # MIGRATED: on_my_chat_member translates to LifecycleAdded/Removed/Migrated and dispatches via on_added/on_removed/on_migrated; bot_user_id
│   ├── discord.py             # EXTENDED: on_guild_join/on_guild_remove, audit-log helper, leave_chat impl, get_invite_link, bot_user_id
│   └── slack.py               # EXTENDED: bot_user_id only (lifecycle stub; spec 008 fills it in)
├── collaborative.py           # EXTENDED: CollabWorkspace.platform, chat_id:str, expected_platform; CollabStore._chat_map (platform, chat_id); find_active_in_guild, find_active_by_platform
├── handlers.py                # REFACTORED: collab_bot_added/removed/migrated signatures use LifecycleAdded/Removed/Migrated; _parse_user_id accepts Discord/Slack mention forms; /im-the-owner handler; leave_chat policy gate
├── authorization.py           # EXTENDED: get_user_role + is_authorised_adder accept user_id: int | str
├── bot.py                     # CHANGED: on_my_chat_member moved into telegram adapter; on_guild_join/on_guild_remove guards removed; lifecycle callbacks registered uniformly
├── migrations/
│   └── v0_28_0.py             # NEW: platform/chat_id-str migration (idempotent)
├── config.py                  # EXTENDED: DISCORD_INVITE_TTL_DAYS, DISCORD_INVITE_MAX_USES env reads
└── i18n.py                    # EXTENDED: new strings (discord_audit_log_unavailable, discord_audit_log_empty, im_the_owner_*); REMOVED: collab_unsupported_platform_discord

tests/
├── test_collab_chat_ref.py             # NEW: ChatRef serialization, hashing, encodings
├── test_collab_discord_lifecycle.py    # NEW: on_guild_join/on_guild_remove → LifecycleAdded/Removed with mock discord.Client
├── test_collab_discord_invite.py       # NEW: get_invite_link uses env vars; fallback on invalid values
├── test_collab_im_the_owner.py         # NEW: golden error messages; success path
├── test_migration_v0_28_0.py           # NEW: idempotency, int→str coercion, default platform="telegram"
├── test_collaborative.py               # EDIT: chat_id is str now; legacy int still loads
├── test_collab_handlers.py             # EDIT: LifecycleAdded payload; platform-agnostic dispatch
├── test_collab_lifecycle.py            # EDIT: signature regression
└── test_collab_multiplatform.py        # EDIT: Discord leave_chat now supported

docs/
├── team.md                    # EDIT: "Collaborative workspaces — Discord" subsection
├── configuration.md           # EDIT: DISCORD_INVITE_TTL_DAYS, DISCORD_INVITE_MAX_USES, required permissions
└── architecture.md            # EDIT: lifecycle-abstraction block

data/                          # RUNTIME (not checked in)
└── collaborative_workspaces.json   # SCHEMA EXTENDED: platform, chat_id:str, expected_platform
```

**Structure Decision**: Single-project layout (existing Robyx pattern). One new migration file, one new handler command, three new lifecycle dataclasses. All other changes are additive edits to existing modules.

## Phase Outline

Detailed task list is in [tasks.md](./tasks.md). High-level phases:

- **Phase 1 — Foundational (T001–T014)**: ChatRef + LifecycleAdded/Removed/Migrated in `base.py`; CollabWorkspace schema extension; CollabStore multi-platform `_chat_map`; migration `v0_28_0`; foundational tests. Telegram remains green. This phase is the spec-007 P1 prerequisite and the spec-008 P1 prerequisite (008 will reuse all of this verbatim).
- **Phase 2 — Discord lifecycle plumbing (T015–T020)**: `on_guild_join`/`on_guild_remove` in `bot/messaging/discord.py`; audit-log helper with 3-retry backoff; `bot_user_id` property; channel-pick policy.
- **Phase 3 — Handler refactor (T021–T025)**: rewrite `collab_bot_added/removed/migrated` to accept `LifecycleAdded/Removed/Migrated`; all branching via `event.chat_ref.platform`; Telegram tests stay green.
- **Phase 4 — Authorization + Flow A on Discord + `/im-the-owner` (T026–T029)**: `is_authorised_adder` accepts `int | str`; pending-workspace matcher honors `expected_platform`; `/im-the-owner` command in handlers.
- **Phase 5 — Mention parsing (T030–T032)**: `_parse_user_id` accepts Discord `<@id>` and `<@!id>`, Slack `<@U...>` (stub for 008), Telegram legacy.
- **Phase 6 — `leave_chat` policy + invite-link (T033–T034)**: `find_active_in_guild` consultation in handler; `get_invite_link` on Discord adapter; `DISCORD_INVITE_TTL_DAYS`/`DISCORD_INVITE_MAX_USES` env reads.
- **Phase 7 — i18n + docs (T035–T038)**: new STRINGS keys; remove `collab_unsupported_platform_discord`; targeted docs edits.
- **Phase 8 — Test coverage completion (T039–T048)**: full Discord lifecycle test sweep + Telegram regression.
- **Phase 9 — Release prep (T049–T051)**: VERSION→0.28.0, CHANGELOG entry, `releases/v0.28.0.md` notes. **Only after every prior phase passes**. Spec 007 alone does not cut the release if 008/009 are co-scheduled — that is a release-planning decision out of scope here.

## Complexity Tracking

No Constitution Check violations. Spec 007 explicitly **closes** the violation declared in spec 003 — the design rests on the same patterns (`Platform` ABC, `CollabStore`, `bot/migrations/v0_*.py`) already proven elsewhere in the codebase. The only architectural addition is the `ChatRef` + lifecycle event dataclasses, which are minimal frozen dataclasses with no behavior beyond data carriage.
