# Requirements checklist — spec 007

A reviewer's checklist before the spec is approved for implementation. Each
item maps back to a functional requirement (FR-XXX) or success criterion
(SC-XXX) in `spec.md`.

## Foundational

- [ ] FR-001 `ChatRef` dataclass defined in `bot/messaging/base.py`, frozen + hashable + JSON-serialisable.
- [ ] FR-002 `LifecycleAdded` / `LifecycleRemoved` / `LifecycleMigrated` dataclasses defined.
- [ ] FR-003 `Platform.on_added`, `on_removed`, `on_migrated` callable attributes available on the ABC.
- [ ] FR-004 `Platform.bot_user_id` property available with sensible default (`None`).

## Schema and migration

- [ ] FR-005 `CollabWorkspace.platform`, `chat_id: str`, `expected_platform` fields added; `from_dict` accepts legacy `int` chat_id.
- [ ] FR-006 `CollabStore._chat_map` keyed by `(platform, chat_id)`; `get_by_chat_id`/`update_chat_id`/`migrate_chat_id` accept `ChatRef`.
- [ ] FR-006 `find_active_in_guild` and `find_active_by_platform` helpers exposed.
- [ ] FR-007 `bot/migrations/v0_28_0.py` ships in the migration chain (`from_version="0.27.2"` → `to_version="0.28.0"`).
- [ ] FR-007 Migration is idempotent (done-marker + per-record no-op when already migrated).

## Discord adapter

- [ ] FR-008 `bot/messaging/discord.py` implements `bot_user_id`, `leave_chat`, `get_invite_link`.
- [ ] FR-009 `on_guild_join` / `on_guild_remove` translate to `LifecycleAdded` / `LifecycleRemoved` and call `self.on_added` / `self.on_removed`.
- [ ] FR-009 `_resolve_inviter` helper with 3-retry exponential backoff (1s/2s/4s); fail-closed on `Forbidden`.
- [ ] FR-010 `_pick_writable_channel` helper; bot leaves guild if no writable channel exists.

## Handlers

- [ ] FR-011 `collab_bot_added/removed/migrated` accept `LifecycleAdded/Removed/Migrated`.
- [ ] FR-011 Branching only via `event.chat_ref.platform`, never `isinstance(platform, ...)`.
- [ ] FR-012 `bot/bot.py:on_guild_join` "not yet supported" guard removed; `STRINGS["collab_unsupported_platform_discord"]` removed.

## Authorization

- [ ] FR-013 `expected_platform` mismatch refuses pending-workspace binding.
- [ ] FR-014 `is_authorised_adder`, `get_user_role` accept `user_id: int | str`.
- [ ] FR-015 `/im-the-owner` command implemented with 5 deterministic refusal STRINGS.

## leave_chat + invite

- [ ] FR-016 Shared-guild policy in handler: `find_active_in_guild` consultation before `leave_chat`.
- [ ] FR-017 `DISCORD_INVITE_TTL_DAYS` and `DISCORD_INVITE_MAX_USES` env vars read in `bot/config.py` with safe defaults.

## Mentions

- [ ] FR-018 `_parse_user_id` accepts Discord `<@id>`, `<@!id>`, Slack `<@U...>` (stub), Telegram legacy.
- [ ] FR-019 `CollabWorkspace.set_role/get_role/remove_user/is_owner/can_execute` accept `int | str`.

## Docs (scoped to 007)

- [ ] FR-020 `docs/team.md` "Collaborative workspaces — Discord" subsection added.
- [ ] FR-021 `docs/configuration.md` documents the two new env vars and Discord permissions.
- [ ] FR-022 `docs/architecture.md` includes lifecycle-abstraction block.

## Success criteria verification

- [ ] SC-001 Grep confirms no `collab_unsupported_platform_discord` STRING and no Discord-specific guard in `bot.py`.
- [ ] SC-002 Manual Flow A on a real Discord guild (quickstart §2) succeeds.
- [ ] SC-003 Manual audit-log failure recovery (quickstart §3) succeeds.
- [ ] SC-004 Shared-guild leave_chat test (`test_leave_chat_skipped_when_shared_guild`) passes.
- [ ] SC-005 Migration runs in <1s on synthetic 50-record state; idempotent.
- [ ] SC-006 `pytest tests/test_collab*` passes 100%.
- [ ] SC-007 `grep -rn 'isinstance.*DiscordPlatform' bot/handlers.py` returns no matches.
- [ ] SC-008 Mention parser unit tests cover all 6 documented input shapes.
- [ ] SC-009 Pre-007 JSON round-trips through migration to byte-equivalent post-007 JSON (modulo new fields).

## Documentation closure

- [ ] Spec 003's `FR-013 justified violation` is referenced and explicitly closed for Discord in `spec.md`.
- [ ] Spec 008 prerequisites documented (this spec's abstractions are reusable verbatim).
