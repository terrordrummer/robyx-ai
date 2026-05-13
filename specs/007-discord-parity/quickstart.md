# Quickstart — Verifying spec 007 (Discord parity)

Five manual scenarios that exercise the user-visible behavior of spec 007.
Each step lists the trigger and the expected observable outcomes. Use this
checklist after a release cut to validate the Discord lifecycle works end-
to-end on a real Discord guild.

**Prerequisites**: a Discord bot application with the OAuth scopes
`bot applications.commands` and the permissions `View Audit Log`,
`Create Instant Invite`, `Send Messages`, `Manage Channels`. Discord bot
token configured in `.env` (`DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
`DISCORD_CONTROL_CHANNEL_ID`).

A second testing guild "robyx-test-007" with two text channels `#general`
and `#project-a` is recommended. The bot owner is Roberto (`OWNER_ID` set to
his Telegram id; his Discord id is recorded as `OWNER_ID_DISCORD` for
manual cross-referencing).

---

## §1 — Migration `v0_28_0` on a pre-007 install

**Trigger**: Boot Robyx v0.28.0 with a `data/collaborative_workspaces.json`
file from v0.27.2 (records carry `chat_id: int`, no `platform` field).

**Expected**:

1. Migration log: `migration v0_28_0: rewrote N records (M added platform field, K coerced chat_id to str)`.
2. `data/collaborative_workspaces.json` now has every record with
   `"platform": "telegram"` and `"chat_id": "<string>"`.
3. The done marker `data/migrations/v0_28_0.done` exists.
4. Re-boot — second run logs `migration v0_28_0: done marker present, skipping`.
5. All existing Telegram workspaces remain routable: `/workspaces` from
   Telegram HQ lists them; sending a message to any group routes correctly.

---

## §2 — Flow A: pre-announce Discord workspace from Telegram HQ

**Trigger**: In Telegram HQ, instruct the orchestrator: "create a
collaborative workspace called `atlas-007` for atlas testing on Discord;
my Discord user id is `<OWNER_ID_DISCORD>`". Orchestrator emits
`[COLLAB_ANNOUNCE name="atlas-007" display="Atlas Test" purpose="testing 007" platform="discord" creator_discord_id="<OWNER_ID_DISCORD>"]`.

**Expected**:

1. HQ confirmation: `[COLLAB_ANNOUNCE ok: name=atlas-007]`.
2. `data/collaborative_workspaces.json` carries a new record:
   ```json
   {
     "id": "collab-atlas-007",
     "name": "atlas-007",
     "chat_id": "0",
     "platform": "discord",
     "expected_platform": "discord",
     "expected_creator_id": "<OWNER_ID_DISCORD>",
     "status": "pending"
   }
   ```
3. Open the Discord OAuth invite URL; add Robyx to `robyx-test-007` guild.
4. Robyx logs:
   ```
   collab.discord.audit_log.success guild=<id> user=<OWNER_ID_DISCORD>
   collab.match ws_id=collab-atlas-007 chat=<guild>:<channel> ...
   ```
5. The bot posts `collab_welcome_pending` in `#general` (the first writable channel).
6. Telegram HQ receives `collab_bot_added_hq_matched` including the Discord
   invite URL (TTL 7d, max-uses 10).
7. Workspace record updates: `chat_id="<guild>:<channel>"`, `status="active"`,
   `invite_link="https://discord.gg/..."`.

---

## §3 — Audit-log failure → `/im-the-owner` recovery

**Setup**: Remove the `View Audit Log` permission from the Robyx bot in a
second testing guild. Pre-announce a workspace `recovery-007` (Flow A as §2).

**Trigger**: Add Robyx to the second testing guild.

**Expected**:

1. Robyx logs `collab.discord.audit_log.forbidden guild=<id>`.
2. The bot posts `STRINGS["discord_audit_log_unavailable"]` in the first
   writable channel of the second guild.
3. Workspace record remains `status="pending"`, `chat_id="0"`.
4. Roberto types `/im-the-owner recovery-007` in `#general` of the second guild.
5. Bot replies `STRINGS["im_the_owner_success"]`.
6. Same downstream flow as §2.4–§2.7.

**Negative**: A second user (not Roberto) types `/im-the-owner recovery-007`
before Roberto. Bot replies `STRINGS["im_the_owner_creator_mismatch"]`; the
workspace remains pending until Roberto claims it.

---

## §4 — Shared-guild `leave_chat` policy

**Setup**: From §2, workspace `atlas-007` is active in `robyx-test-007:#general`.

**Trigger**: Have an unauthorized Discord user (not Roberto, not OWNER/OPERATOR
in any active workspace) add Robyx to a **second channel** in the same guild
(`robyx-test-007:#project-a`) — Discord OAuth supports per-channel invites
via channel-restricted permissions, but at the gateway level the bot only
sees `on_guild_join` once per guild. For this scenario, simulate by having
the unauthorized user trigger a re-add via removing and re-adding the bot, or
test instead by adding a pending workspace for an unauthorized user id and
having them claim it.

**Expected**:

1. Refusal flow fires: bot posts `STRINGS["collab_unauthorised_adder"]` in
   `#project-a` (the new channel).
2. Handler logs: `collab.leave_chat.skipped guild=<id> active_count=1`.
3. `platform.leave_chat` is **not** called.
4. `atlas-007` remains active in `#general` — confirm with `/workspaces` from
   Telegram HQ.
5. The bot stays a member of `robyx-test-007`.

---

## §5 — Telegram regression

**Trigger**: Run the full Telegram collaborative-workspace test suite:

```bash
cd <repo-root>
PYTHONPATH=bot pytest tests/test_collab* tests/test_collaborative.py tests/test_authorization.py -v
```

**Expected**: All tests pass. Spec 003's golden paths (Telegram Flow A
deep-link, Flow B in-group setup, unauthorized-adder refusal, supergroup
migration) are unchanged. Mention parsing on Telegram (`/promote @123`)
still works.

---

## Test coverage map (spec 007 acceptance scenarios → test files)

| Scenario | Test file |
|---|---|
| US1.1 — Flow A audit-log success | `tests/test_collab_discord_lifecycle.py::test_flow_a_audit_log_match` |
| US1.2 — unauthorized add refusal | `tests/test_collab_discord_lifecycle.py::test_unauth_add_refusal_with_leave` |
| US1.3 — cross-platform refusal (`expected_platform`) | `tests/test_collab_chat_ref.py::test_expected_platform_mismatch_refused` |
| US1.4 — invite link defaults | `tests/test_collab_discord_invite.py::test_invite_link_defaults` |
| US2.1 — audit-log Forbidden fallback | `tests/test_collab_discord_lifecycle.py::test_forbidden_posts_fallback_message` |
| US2.2 — empty audit-log retry | `tests/test_collab_discord_lifecycle.py::test_empty_audit_log_retries_then_fallback` |
| US2.3 — `/im-the-owner` success | `tests/test_collab_im_the_owner.py::test_success_path` |
| US2.4 — creator mismatch refusal | `tests/test_collab_im_the_owner.py::test_creator_mismatch` |
| US2.5 — unknown workspace refusal | `tests/test_collab_im_the_owner.py::test_unknown_workspace` |
| US3.1 — Flow B Discord setup | `tests/test_collab_discord_lifecycle.py::test_flow_b_ad_hoc_setup` |
| US4.1 — bot removed → close workspace | `tests/test_collab_discord_lifecycle.py::test_guild_remove_closes_workspace` |
| US5.1 — shared-guild leave_chat skip | `tests/test_collab_multiplatform.py::test_leave_chat_skipped_when_shared_guild` |
| US6.1 — Discord mention parse | `tests/test_collab_handlers.py::test_parse_user_id_discord_mention` |
| US6.4 — invalid mention | `tests/test_collab_handlers.py::test_parse_user_id_invalid` |
| Migration | `tests/test_migration_v0_28_0.py::test_idempotent_double_run` |
| Migration | `tests/test_migration_v0_28_0.py::test_coerces_int_chat_id_and_adds_platform` |
| Telegram regression | `tests/test_collaborative.py::test_chat_id_string_roundtrip` |
