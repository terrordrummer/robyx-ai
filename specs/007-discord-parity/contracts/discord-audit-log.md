# Contract — Discord audit-log inviter lookup

**Location**: `bot/messaging/discord.py` (helper) and `bot/handlers.py` (fallback prompt)

## Goal

Resolve "who added the bot to this Discord guild" when `on_guild_join` fires.
Discord's gateway does not carry the inviter on the join event, so the bot
must consult the guild's audit log.

## Required Discord permission

The bot's OAuth scope MUST include **`View Audit Log`** (`VIEW_AUDIT_LOG` =
`0x00000080`). Documented in `docs/configuration.md` per FR-021.

Without this permission, `guild.audit_logs(...)` raises
`discord.Forbidden` — the helper treats this as a soft failure and returns
`(None, None)`.

## Helper signature

```python
async def _resolve_inviter(self, guild) -> tuple[int | None, str | None]:
    """Return (inviter_user_id, inviter_username) or (None, None) on failure.

    Retries with exponential backoff (1s, 2s, 4s) to absorb Discord's
    occasional write lag between guild-add and audit-log-write.
    """
```

## Retry policy

| Attempt | Delay before attempt | Total elapsed (worst case) |
|---------|----------------------|----------------------------|
| 1       | 0s                   | 0s                         |
| 2       | 1s                   | 1s                         |
| 3       | 2s                   | 3s                         |
| 4       | 4s                   | 7s                         |

After attempt 4, the helper returns `(None, None)`.

## Failure modes

| Discord behavior | Helper action | Returned value |
|---|---|---|
| `Forbidden` (missing permission) | log WARN, return immediately (no retries) | `(None, None)` |
| Empty audit-log result on all retries | log INFO `audit_log empty for guild=<id>`, return | `(None, None)` |
| Transient API exception | log WARN `audit_log error attempt=N`, retry | retry until exhausted |
| Successful entry on attempt N | return first entry's user id and username | `(user.id, user.name)` |

The "first entry" is the most recent `bot_add` action; discord.py's
`guild.audit_logs(...)` yields entries in newest-first order by default.

## Handler-side fallback

When `_resolve_inviter` returns `(None, None)`, the lifecycle handler MUST:

1. Pick the first text channel in the guild where
   `permissions_for(guild.me).send_messages` is True (helper:
   `_pick_writable_channel(guild)`).
2. Post `STRINGS["discord_audit_log_unavailable"]` (or `discord_audit_log_empty`
   when specifically empty) in that channel.
3. Leave the pending workspace (if any) in `status="pending"` — do NOT
   speculatively bind.
4. Log `collab.discord.audit_log_failed guild=<id> reason=<forbidden|empty|errors>`.

If no writable channel exists, the helper:
1. Logs `collab.discord.no_writable_channel guild=<id>`.
2. Calls `await guild.leave()` — the bot cannot operate in a guild it cannot
   write to.
3. Notifies HQ via the existing `collab_unauthorised_adder_hq` STRING or a
   new `collab_discord_no_channel_hq` STRING (TBD in Phase 7 i18n task).

## i18n strings (new)

```python
"discord_audit_log_unavailable": (
    "I couldn't determine who added me to this Discord guild — please ensure "
    "the bot has the *View Audit Log* permission, or use `/im-the-owner "
    "<workspace-name>` in any channel of this guild to manually claim a "
    "pending workspace."
),
"discord_audit_log_empty": (
    "I couldn't find the audit-log entry for my recent add to this guild. "
    "If you pre-announced a workspace for this guild, type "
    "`/im-the-owner <workspace-name>` here to claim it."
),
"discord_no_writable_channel_hq": (
    "I was added to Discord guild *%s* (`%d`) but I have no writable text "
    "channel there — I have left the guild."
),
```

## Test coverage

- `tests/test_collab_discord_lifecycle.py::test_resolve_inviter_success` — mock
  `guild.audit_logs` yielding one entry; helper returns the entry's user id.
- `test_resolve_inviter_forbidden` — mock `audit_logs` raising `discord.Forbidden`;
  helper returns `(None, None)` after a single attempt (no retry).
- `test_resolve_inviter_empty_then_present` — mock yielding empty on attempt 1
  and 2, then one entry on attempt 3; helper returns the entry's user id.
- `test_resolve_inviter_all_empty` — helper exhausts retries, returns `(None, None)`.
- `test_resolve_inviter_transient_error` — first call raises `RuntimeError`,
  second call yields entry; helper retries and returns.
- `test_handler_audit_log_failure_posts_fallback` — `LifecycleAdded` with
  `added_by_id=None`; handler posts `discord_audit_log_unavailable` message
  in the first writable channel.

## Observability

All `_resolve_inviter` outcomes log at INFO with `collab.discord.audit_log.*`
prefixes for grep-friendly auditing:

- `collab.discord.audit_log.success guild=<id> user=<id>`
- `collab.discord.audit_log.forbidden guild=<id>`
- `collab.discord.audit_log.empty guild=<id> attempts=<n>`
- `collab.discord.audit_log.error guild=<id> attempt=<n> err=<repr>`
