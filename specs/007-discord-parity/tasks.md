---
description: "Task list for spec 007 — Discord parity for collaborative workspaces"
---

# Tasks: Discord parity for collaborative workspaces

**Input**: Design documents from `/specs/007-discord-parity/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (chat-ref, lifecycle-events, discord-audit-log, im-the-owner), quickstart.md

**Tests**: REQUIRED for every contract (Constitution Principle IV). Every implementation task has a paired test task in its phase. Telegram regression runs at the end of every phase.

**Organization**: Tasks grouped by phase (1–9). Phase 1 is foundational and shared with spec 008. Phase 9 is release; do not enter it until 8 is green.

## Format: `[ID] [P?] [Phase] Description`

- **[P]** — can run in parallel (different files, no dependencies)
- **[Phase]** — phase identifier (P1, P2, ..., P9)

## Path conventions

- Source: `bot/` at repo root
- Tests: `tests/` at repo root
- Migration: `bot/migrations/v0_28_0.py`
- Spec: `specs/007-discord-parity/`

---

## Phase 1 — Foundational schema, abstractions, migration (T001–T014)

**Purpose**: Introduce the platform-agnostic abstractions and schema changes that spec 008 reuses. Telegram remains fully functional after this phase.

**Checkpoint**: After T014, run `pytest tests/test_collab*` and `ruff check bot/ tests/`. Both must pass before proceeding to Phase 2.

- [ ] T001 [P1] Add `ChatRef` frozen dataclass to `bot/messaging/base.py` per `contracts/chat-ref.md`: fields `platform: str`, `chat_id: str`; methods `to_dict`, `from_dict`. Export from the module's `__all__`.
- [ ] T002 [P1] Add `LifecycleAdded`, `LifecycleRemoved`, `LifecycleMigrated` frozen dataclasses to `bot/messaging/base.py` per `contracts/lifecycle-events.md`.
- [ ] T003 [P1] Add `on_added`, `on_removed`, `on_migrated` typed callable attributes (default `None`) and `bot_user_id` property (default `None`) to the `Platform` ABC in `bot/messaging/base.py`. Update module docstring to note that adapters dispatch lifecycle events through these attributes.
- [ ] T004 [P1] Extend `CollabWorkspace` in `bot/collaborative.py` per `data-model.md` §1: add `platform: str = "telegram"`, change `chat_id` type-hint and default to `str = "0"`, add `expected_platform: str | None = None`. Add a `chat_ref` property returning `ChatRef(platform=self.platform, chat_id=self.chat_id)`.
- [ ] T005 [P1] Update `CollabWorkspace.to_dict` to emit `chat_id` as `str` and include `platform` and `expected_platform`. Update `CollabWorkspace.from_dict` to: accept legacy `chat_id: int` (coerce to `str`) AND new `chat_id: str`; default `platform` to `"telegram"`; default `expected_platform` to `None`. Widen `set_role`, `get_role`, `remove_user`, `is_owner`, `can_execute`, `created_by`, `expected_creator_id` type hints to `int | str | None` where applicable (stringification on store is already string-keyed).
- [ ] T006 [P1] Add `parse_discord_chat_id`, `make_discord_chat_id` helpers in `bot/collaborative.py` per `contracts/chat-ref.md`. Slack helpers are deferred to spec 008.
- [ ] T007 [P1] Change `CollabStore._chat_map` to `dict[tuple[str, str], str]` in `bot/collaborative.py`. Update `_rebuild_chat_map` to key by `(ws.platform, ws.chat_id)` for routable statuses. Update the existing `chat_ids` property to return a `set[tuple[str, str]]` and audit callers (the only caller in `bot/handlers.py` rewrites alongside the lifecycle refactor in Phase 3).
- [ ] T008 [P1] Change `CollabStore.get_by_chat_id`, `update_chat_id`, `migrate_chat_id` signatures to accept `ChatRef` (and `chat_ref` semantics: split keys via `(chat_ref.platform, chat_ref.chat_id)`). `expected_creator_id` widens to `int | str | None`. Telegram callers in `bot/handlers.py` and `bot/bot.py` are updated in Phase 3.
- [ ] T009 [P1] Add `CollabStore.find_active_in_guild(guild_id: str) -> list[CollabWorkspace]` in `bot/collaborative.py`: returns active+setup workspaces where `platform=="discord"` and `chat_id` starts with `f"{guild_id}:"`.
- [ ] T010 [P1] Add `CollabStore.find_active_by_platform(platform: str) -> list[CollabWorkspace]` in `bot/collaborative.py`: returns active+setup workspaces filtered by platform. Reserved for spec 008 but harmless for 007.
- [ ] T011 [P1] Update `bot/collaborative.py` `create_pending` to accept an optional `platform: str = "telegram"` and `expected_platform: str | None = None`; default the field on creation. Update `list_pending_for_creator` to optionally accept a `platform` filter (default `None` = any platform — preserves Telegram callers).
- [ ] T012 [P1] Create `bot/migrations/_pending_v0_28_0.py` per `data-model.md` §6 and `research.md` R-MIG-01: from_version `"0.27.2"`, to_version `"0.28.0"`. Logic: read `data/collaborative_workspaces.json`; for each record set `platform="telegram"` if missing and coerce `chat_id` to `str`; atomic write. Done marker at `data/migrations/v0_28_0.done`. The `Migration` constant goes in the module's `MIGRATION` field per the existing pattern (see `bot/migrations/v0_27_0.py`). **Parking note**: filename is `_pending_v0_28_0.py` (leading underscore) so the discovery `_MODULE_PATTERN` does not pick it up while VERSION is still `0.27.2`. Phase 9 (T049) renames it to `v0_28_0.py` alongside the VERSION bump in the same commit. The in-process compat path (`__post_init__` + `from_dict`) handles legacy records throughout Phase 1–8.
- [ ] T013 [P1] Create `tests/test_collab_chat_ref.py`: ChatRef equality + hashing, encodings round-trip via to_dict/from_dict, `parse_discord_chat_id` happy path + malformed input, `make_discord_chat_id` formatting, expected_platform mismatch refusal via store helper.
- [ ] T014 [P1] Create `tests/test_migration_v0_28_0.py`: synthetic pre-007 JSON (3 records: one with legacy int chat_id, one with already-string chat_id but no platform field, one already migrated) → run migration → assert post-state correct; second run is a no-op (done marker check); idempotency on already-migrated single record.
- [ ] T014a [P1] Update `tests/test_collaborative.py` to construct workspaces with `chat_id: str`; add a test that explicitly verifies `from_dict` accepts a legacy `{"chat_id": -1001234}` record and coerces to `"-1001234"`.

**Checkpoint**: run `cd <repo-root> && PYTHONPATH=bot pytest tests/test_collab* tests/test_collaborative.py tests/test_authorization.py -v` and `ruff check bot/ tests/`. Both green. Then await user review before Phase 2.

---

## Phase 2 — Discord lifecycle plumbing (T015–T020)

**Purpose**: Wire Discord's native lifecycle events into the abstractions.

- [ ] T015 [P2] Implement `DiscordPlatform.bot_user_id` in `bot/messaging/discord.py`: return `self._client.user.id` if `self._client` and `self._client.user` are set, else `None`.
- [ ] T016 [P2] Implement `DiscordPlatform._resolve_inviter(guild) -> tuple[int | None, str | None]` per `contracts/discord-audit-log.md`. 3-retry exponential backoff (1s/2s/4s); fail-closed on `discord.Forbidden`.
- [ ] T017 [P2] Implement `DiscordPlatform._pick_writable_channel(guild)` returning the first text channel where `permissions_for(guild.me).send_messages` is True, or `None`.
- [ ] T018 [P2] Register `on_guild_join` event in `DiscordPlatform`: resolves inviter, picks channel, leaves guild if no writable channel, otherwise emits `LifecycleAdded` via `self.on_added`.
- [ ] T019 [P2] Register `on_guild_remove` event in `DiscordPlatform`: emits `LifecycleRemoved` via `self.on_removed`. (The handler iterates `find_active_in_guild` and closes each matching workspace.)
- [ ] T020 [P2] Add `DiscordPlatform.register_lifecycle(client)` (covered by T018/T019) and the `_wire_lifecycle(plat, h)` helper to `bot/bot.py` (defined but NOT YET invoked from `_run_discord`). **Removal of the "not yet supported on Discord" guard and the call to `register_lifecycle`/`_wire_lifecycle` from `_run_discord` are deferred to Phase 3 (T023)** because the existing `collab_bot_*` handlers still use the pre-spec-007 signature `(platform, chat, added_by)`; switching to the new dispatch shape requires the handler refactor in T021–T022 to land in the same commit. Phase 2 ships the adapter plumbing additively so it is testable in isolation.

---

## Phase 3 — Handler refactor to platform-agnostic dispatch (T021–T025)

**Purpose**: Rewrite the `collab_bot_*` handlers to accept lifecycle dataclasses and remove all isinstance branching.

- [ ] T021 [P3] Move `_on_my_chat_member` logic out of `bot/bot.py:_run_telegram` into a new `TelegramPlatform._register_lifecycle(application)` method in `bot/messaging/telegram.py`. The method registers the PTB `ChatMemberHandler` internally and emits `LifecycleAdded` / `LifecycleRemoved` / `LifecycleMigrated` via `self.on_added/on_removed/on_migrated`. `bot.py` calls `_register_lifecycle(app)` once after `_wire_lifecycle(plat, h)`.
- [ ] T022 [P3] Change `collab_bot_added`, `collab_bot_removed`, `collab_bot_migrated` signatures in `bot/handlers.py` to `(platform, event: LifecycleAdded|Removed|Migrated)`. Rewrite the bodies to read `chat_id = event.chat_ref.chat_id`, `added_by_id = event.added_by_id`, `chat_title = event.chat_title`. Remove all `chat.id` / `added_by.id` access.
- [ ] T023 [P3] `bot/bot.py`: `_wire_lifecycle(plat, h)` helper already defined in Phase 2 T020 — invoke it from `_run_telegram`, `_run_discord`, `_run_slack` (last is a no-op since Slack does not emit lifecycle in 007 — but the wiring is in place for 008). For `_run_discord`, also call `plat.register_lifecycle(client)` and DELETE the old `@client.event on_guild_join` "not yet supported" guard in the same commit as T022 (handler signature refactor).
- [ ] T024 [P3] Update every caller in `bot/handlers.py` that constructs a `ChatRef` from `chat_id`. Replace `collab_store.get_by_chat_id(chat_id)` with `collab_store.get_by_chat_id(ChatRef(platform=event.chat_ref.platform, chat_id=event.chat_ref.chat_id))` (or pass `event.chat_ref` directly).
- [ ] T025 [P3] Update existing tests `tests/test_collab_handlers.py`, `tests/test_collab_lifecycle.py`, `tests/test_collab_multiplatform.py` to construct `LifecycleAdded` / `LifecycleRemoved` / `LifecycleMigrated` payloads instead of bare `chat` and `added_by` mocks. Run the three test modules to confirm Telegram regression remains green.

---

## Phase 4 — Authorization, Flow A on Discord, `/im-the-owner` (T026–T029)

**Purpose**: Cross-platform authorization, audit-log-based Flow A, manual claim escape hatch.

- [ ] T026 [P4] Widen `is_authorised_adder`, `get_user_role` type hints in `bot/authorization.py` to `user_id: int | str | None`. No behavior change for Telegram; preserves callers.
- [ ] T027 [P4] Flow A matcher in `collab_bot_added`: when `event.chat_ref.platform != "telegram"`, look up pending workspaces by `expected_creator_id == event.added_by_id` AND `expected_platform == event.chat_ref.platform`. Refuse cross-platform binds with `log.info("collab.match.platform_mismatch ...")`.
- [ ] T028 [P4] Extract `_flow_a_post_bind(platform, ws, chat_ref)` helper in `bot/handlers.py` capturing the post-bind steps (set OWNER role, generate invite, post welcome, notify HQ). Reused by both the audit-log success path and `/im-the-owner`.
- [ ] T029 [P4] Implement `_handle_im_the_owner(platform, msg, msg_ref)` per `contracts/im-the-owner.md`. Register in the `on_message` dispatch path on Discord (and Slack — Slack lookup body will work as-is since 008 only adds adapter-side code). Add new i18n STRINGS: `im_the_owner_unknown_workspace`, `im_the_owner_already_bound`, `im_the_owner_platform_mismatch`, `im_the_owner_creator_mismatch`, `im_the_owner_no_pending`, `im_the_owner_success`.
- [ ] T029a [P4] Create `tests/test_collab_im_the_owner.py`: success path, unknown workspace, already-bound, platform mismatch (pending ws platform="telegram"), creator mismatch, usage error.

---

## Phase 5 — Cross-platform mention parser (T030–T032)

**Purpose**: `_parse_user_id` accepts Discord and Slack mention syntax.

- [ ] T030 [P5] Rewrite `_parse_user_id` in `bot/handlers.py` per `research.md` R-CMD-01: dialect table for Discord `<@id>` / `<@!id>`, Slack `<@U...>` (returns str), Telegram legacy (`@username`, `@123`, `123`).
- [ ] T031 [P5] Update the role-management handler chain (`/promote`, `/demote`, etc.) to handle `str` return values from `_parse_user_id` — `CollabWorkspace.set_role/get_role` already accept `int | str` after T005, so the callers only need to stop forcing `int`.
- [ ] T032 [P5] Add tests in `tests/test_collab_handlers.py` for all 6 documented input shapes: Discord `<@123>`, Discord `<@!123>`, Slack `<@U12345>`, Telegram `@username` (returns None — alphanumeric), Telegram `@123`, bare `123`.

---

## Phase 6 — `leave_chat` policy + invite-link config (T033–T034)

- [ ] T033 [P6] Implement `DiscordPlatform.leave_chat(chat_id)` per `research.md` R-LEAVE-01: parse `<guild>:<channel>`, fetch guild, call `guild.leave()`. Implement `DiscordPlatform.get_invite_link(chat_id)`: parse the chat_id, fetch the channel, call `channel.create_invite(max_age=DISCORD_INVITE_TTL_DAYS*86400, max_uses=DISCORD_INVITE_MAX_USES, reason="Robyx collaborative workspace invite")`.
- [ ] T034 [P6] Shared-guild policy in `bot/handlers.py:collab_bot_added` unauthorized-adder guard: before `platform.leave_chat(chat_ref)`, check `collab_store.find_active_in_guild(guild_id)`. If any other active workspace shares the guild, skip `leave_chat` and log `collab.leave_chat.skipped guild=... active_count=...`. Add `DISCORD_INVITE_TTL_DAYS` and `DISCORD_INVITE_MAX_USES` to `bot/config.py` with sane defaults (7, 10).
- [ ] T034a [P6] Create `tests/test_collab_discord_invite.py`: invite link uses configured env vars; invalid env values fall back to defaults with WARN log.

---

## Phase 7 — i18n + targeted docs (T035–T038, parallelizable)

- [ ] T035 [P7] [P] Add new i18n STRINGS in `bot/i18n.py`: `discord_audit_log_unavailable`, `discord_audit_log_empty`, `discord_no_writable_channel_hq`, `im_the_owner_*` (5 keys + success), `collab_match_platform_mismatch_log` (optional, for log strings). REMOVE `collab_unsupported_platform_discord`.
- [ ] T036 [P7] [P] Update `docs/team.md` with "Collaborative workspaces — Discord" subsection covering Flow A, Flow B, audit-log permission, `/im-the-owner`, invite link defaults, shared-guild leave policy.
- [ ] T037 [P7] [P] Update `docs/configuration.md` with `DISCORD_INVITE_TTL_DAYS`, `DISCORD_INVITE_MAX_USES`, and the required Discord permissions (`View Audit Log`, `Create Instant Invite`, `Send Messages`, `Manage Channels`).
- [ ] T038 [P7] [P] Update `docs/architecture.md` with a short lifecycle-abstraction block describing `Platform.on_added/on_removed/on_migrated`, `ChatRef`, and the dispatch flow `adapter → LifecycleAdded → handler`.

---

## Phase 8 — Full Discord lifecycle test coverage + Telegram regression (T039–T048)

- [ ] T039 [P8] Create `tests/test_collab_discord_lifecycle.py` skeleton: shared fixtures for mock `discord.Client`, `discord.Guild`, audit-log entries.
- [ ] T040 [P8] `test_flow_a_audit_log_match` — pending ws + audit-log returns matching inviter → workspace bound, role set, welcome posted, HQ notified.
- [ ] T041 [P8] `test_forbidden_posts_fallback_message` — `_resolve_inviter` raises `discord.Forbidden` → fallback message posted in first writable channel.
- [ ] T042 [P8] `test_empty_audit_log_retries_then_fallback` — audit-log empty 3 times → fallback message; mock asserts 3 sleeps with 1s/2s/4s args.
- [ ] T043 [P8] `test_unauth_add_refusal_with_leave` — unauthorized adder, no other active workspace in guild → refusal in chan, `leave_chat` called.
- [ ] T044 [P8] `test_leave_chat_skipped_when_shared_guild` — unauthorized adder, another active workspace in same guild → refusal posted but `leave_chat` NOT called.
- [ ] T045 [P8] `test_flow_b_ad_hoc_setup` — authorized adder, no pending workspace → provisional workspace created with `platform="discord"`, `status="setup"`, bootstrap prompt posted.
- [ ] T046 [P8] `test_guild_remove_closes_workspace` — `on_guild_remove` → `find_active_in_guild` returns one ws → workspace closed.
- [ ] T047 [P8] `test_cross_platform_refusal` — pending ws `expected_platform="discord"`; same `expected_creator_id` adds bot to a Telegram group → not bound (cross-platform refusal).
- [ ] T048 [P8] Run full Telegram regression: `pytest tests/test_collab* tests/test_collaborative.py tests/test_authorization.py -v` → 100% pass.

---

## Phase 9 — Release prep (T049–T051) — DO NOT START until P8 is green

- [ ] T049 [P9] Bump `VERSION` to `0.28.0` AND rename `bot/migrations/_pending_v0_28_0.py` → `bot/migrations/v0_28_0.py` in the same commit (so the chain validator picks up the parked migration). Update the import in `tests/test_migration_v0_28_0.py` from `_pending_v0_28_0` back to `v0_28_0`. Add CHANGELOG entry summarising spec 007 (Discord lifecycle, ChatRef, migration, `/im-the-owner`, invite link, shared-guild leave policy).
- [ ] T050 [P9] Create `releases/v0.28.0.md` release notes covering: closure of spec 003 FR-013 for Discord, new Discord env vars, new STRINGS, migration behavior, manual-claim escape hatch, the Slack stub (lifecycle still pending spec 008).
- [ ] T051 [P9] Final pre-tag audit: `ruff check bot/ tests/`, `pytest -x`, manual quickstart §1–§5 on a real Discord guild, `grep` audit (no `isinstance.*DiscordPlatform` in `bot/handlers.py`, no `collab_unsupported_platform_discord` in `bot/i18n.py`).

---

## Dependencies and execution order

- **Phase 1** — no dependencies.
- **Phase 2** — requires Phase 1 (lifecycle dataclasses, ChatRef).
- **Phase 3** — requires Phase 2 (Discord emits events) AND Phase 1 (ChatRef shape).
- **Phase 4** — requires Phase 3 (handler signature stable).
- **Phase 5** — requires Phase 1 (CollabWorkspace.set_role widening); can run in parallel with Phase 4.
- **Phase 6** — requires Phase 3 + Phase 4 (handler structure stable).
- **Phase 7** — can run in parallel with Phase 6 once strings and behaviors are stable; the i18n addition in T029 (Phase 4) is a prerequisite for T035.
- **Phase 8** — requires every prior phase complete.
- **Phase 9** — requires Phase 8 green.

## Story independence

- Phase 1 + Phase 4 + Phase 5 are reusable verbatim by spec 008 (Slack). No 008-specific code is mixed into 007.

## Parallelisation hints

- Within Phase 1: T013, T014, T014a can run in parallel after T001–T012 land.
- Within Phase 7: T035/T036/T037/T038 modify different files and can run in parallel.
- Within Phase 8: T040–T047 are independent test modules and can be drafted in parallel by separate developers.

## Notes

- Telegram regression MUST pass at every checkpoint (end of each Phase).
- Migration is the riskiest piece; T012 and T014 must be reviewed together.
- Spec 008's task plan will reuse Phase 1's foundational abstractions verbatim; do not let 007-only code creep into Phase 1 outputs.
- Phase 9 release artifacts (VERSION, CHANGELOG, releases/v0.28.0.md) are explicitly the last step. Spec 007 does NOT cut a release on its own if spec 008/009 are co-scheduled in the same cycle.
