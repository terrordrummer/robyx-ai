# Phase 0 Research — Discord Parity for Collaborative Workspaces

Resolves the technical unknowns introduced by spec 007. Each decision below
feeds directly into the Phase 1 artifacts and the Phase 2 task breakdown.

---

## R-LC-01 — Lifecycle event abstraction (observer pattern on `Platform` ABC)

**Decision**: Add three optional Awaitable callback attributes to the `Platform` ABC:

```python
Platform.on_added:    Callable[[LifecycleAdded],    Awaitable[None]] | None
Platform.on_removed:  Callable[[LifecycleRemoved],  Awaitable[None]] | None
Platform.on_migrated: Callable[[LifecycleMigrated], Awaitable[None]] | None
```

Each adapter is responsible for translating its **native** lifecycle event
(`ChatMemberHandler` on Telegram, `on_guild_join`/`on_guild_remove` on Discord,
`member_joined_channel` on Slack — stubbed in 007, filled in 008) into the
matching dataclass and awaiting the registered callback.

In `bot.py`, registration happens once, agnostically:

```python
plat.on_added    = lambda evt: h["collab_bot_added"](plat, evt)
plat.on_removed  = lambda evt: h["collab_bot_removed"](plat, evt)
plat.on_migrated = lambda evt: h["collab_bot_migrated"](plat, evt)
```

**Rationale**:

- The current pattern wires Telegram's PTB `ChatMemberHandler` directly inside
  `bot.py:_run_telegram`. That tight coupling is the reason spec 003 had to
  guard Discord/Slack with platform-specific "not yet supported" notices — the
  abstraction was missing. The observer pattern decouples lifecycle event
  emission from the dispatch site.
- Spec 008 will register `Slack.on_added` identically. Zero code in `bot.py` or
  `handlers.py` needs to change.
- Frozen dataclasses for events (vs free-form dicts) means refactors are caught
  by mypy and runtime `dataclasses.FrozenInstanceError`, not silently mis-keyed.

**Alternatives considered**:

- **Event-bus / pubsub module**: introducing `bot/events_bus.py` with topic
  subscriptions. Rejected — overkill for 3 events, adds a new abstraction layer
  with no current consumer beyond the collab module.
- **Adapter inheritance** (each adapter inherits from a `LifecycleEmitter`
  mixin): rejected — diamond inheritance with `Platform` ABC is messier than
  three plain attributes.
- **Keep dispatch in `bot.py` with `isinstance` branching per platform**:
  rejected — spec 008 would have to add another branch, doubling the risk of
  drift between Telegram and Discord/Slack lifecycle handling.

---

## R-DM-01 — Data model: `chat_id` as `str`, `platform` as field

**Decision**:

```python
@dataclass
class CollabWorkspace:
    id: str
    name: str
    display_name: str
    agent_name: str
    chat_id: str = "0"             # CHANGED from int; "0" = pending unbound
    platform: str = "telegram"     # NEW; back-compat default
    expected_platform: str | None = None  # NEW; pending workspaces refuse cross-platform binding
    # ... existing fields unchanged
```

Canonical `chat_id` encodings:

| Platform | Form               | Example                          |
|----------|--------------------|----------------------------------|
| Telegram | `"<chat_id_int>"`  | `"-1001234567890"`               |
| Discord  | `"<guild>:<chan>"` | `"123456789012345678:987654321098765432"` |
| Slack    | `"<team>:<chan>"`  | `"T01ABC:C02DEF"` (reserved for 008) |

**Rationale**:

- A single string field absorbs all three platforms without union types or
  variant tables. Comparison, hashing, and JSON serialisation are trivial.
- Telegram's existing numeric chat ids serialize losslessly to `str(int)` and
  back. The migration `v0_28_0` performs the in-place coercion.
- `platform="telegram"` default preserves the loading semantics of every
  pre-007 record. `from_dict` accepts both legacy `int` and new `str` chat_id
  (coercion happens on load).
- `expected_platform` is the cross-platform user-id collision guard. Without
  it, a Telegram user `123` could hijack a pending workspace created with
  `expected_creator_id=123` on Discord. With it, the matcher refuses unless
  both creator and platform match.

**Alternatives considered**:

- **Variant per platform** (`TelegramCollabWorkspace`, `DiscordCollabWorkspace`):
  rejected — explodes the type surface in `CollabStore` and breaks the existing
  `add/remove/close/get` symmetry.
- **Compound key `(platform, chat_id)` exposed as separate fields**: that IS
  what we do internally (`_chat_map` is keyed by the tuple), but the workspace
  record stores the canonical string form to keep JSON readable and
  human-greppable.
- **`platform` derived from `chat_id` shape**: rejected — would require parsing
  every chat_id every lookup, and breaks for edge cases (Telegram channel ids
  could in principle contain colons in some future evolution). Explicit field is
  clearer.

---

## R-AUTH-01 — Discord audit-log lookup with retry + fail-closed

**Decision**:

```python
async def _resolve_inviter(guild) -> int | None:
    import discord
    delays = (1.0, 2.0, 4.0)
    for attempt, delay in enumerate(delays, start=1):
        try:
            entries = [e async for e in guild.audit_logs(
                action=discord.AuditLogAction.bot_add, limit=5,
            )]
        except discord.Forbidden:
            log.warning("audit_log Forbidden in guild %s (attempt %d)", guild.id, attempt)
            return None
        except Exception as exc:
            log.warning("audit_log error in guild %s (attempt %d): %s", guild.id, attempt, exc)
            if attempt < len(delays):
                await asyncio.sleep(delay)
                continue
            return None
        if entries:
            # Most recent bot_add wins (entries iterate newest-first per discord.py).
            return entries[0].user.id
        if attempt < len(delays):
            await asyncio.sleep(delay)
    return None
```

Required Discord permission: **`View Audit Log`**. Documented in
`docs/configuration.md` (FR-021).

On `_resolve_inviter() is None`, the handler:
1. Picks the first writable channel in the guild (`permissions_for(guild.me).send_messages == True`).
2. Posts `STRINGS["discord_audit_log_unavailable"]` (or `_empty` if specifically empty).
3. Leaves the workspace pending. The escape hatch is `/im-the-owner` (R-AUTH-02).

**Rationale**:

- `on_guild_join` on Discord intentionally does not carry the inviter — this
  is a Discord API design choice and not something the adapter can work
  around without privileged intents the bot does not have.
- Audit log is the canonical fallback per discord.py docs and the broader
  bot-developer community. The 3-retry / exponential backoff covers Discord's
  occasional write lag between guild-add and audit-log-write.
- Fail-closed: a missing inviter never silently binds a workspace; the user
  must explicitly claim it. This eliminates the "did the bot bind correctly?"
  ambiguity that plagued early Discord bots.

**Alternatives considered**:

- **OAuth `state` parameter on the bot invite URL**: Discord OAuth `state` is
  only visible to the **inviter's browser**, not to the bot's gateway events.
  The bot would need a web endpoint to receive the redirect. Out of scope for
  a chat-first bot.
- **Privileged Intent `presences`**: not sufficient. The bot would still not
  receive a "you were just invited by user X" event without a web webhook.
- **Polling `guild.members.fetch` to look for "bot was just added by..."**:
  no such API exists. The audit log is the supported path.

---

## R-AUTH-02 — `/im-the-owner` escape hatch

**Decision**: A text command `/im-the-owner <workspace-name>`, parsed by
`bot/handlers.py:on_message` on Discord (and reserved for Slack in spec 008).
The command:

1. Looks up the pending workspace by `name`.
2. Refuses if `ws.platform != "discord"` (`im_the_owner_platform_mismatch`).
3. Refuses if `ws.expected_creator_id != msg.user_id` (`im_the_owner_creator_mismatch`).
4. Refuses if `ws.status != "pending"` (`im_the_owner_already_bound` if
   active, or `im_the_owner_unknown_workspace` if closed/deleted).
5. On success: binds `chat_id="<guild>:<channel>"` (the channel of the command
   message), promotes to `active`, generates invite, posts welcome, notifies
   HQ — identical to the audit-log success path.

**Rationale**:

- Without `/im-the-owner`, every audit-log permission misconfiguration is a
  dead end. The command is a user-visible, deterministic recovery path.
- Symmetric error messages (one per refusal class) keep the UX predictable
  and the test surface bounded (4 explicit error codes).

**Alternatives considered**:

- **Web dashboard**: rejected — violates Principle II (Chat-First Configuration).
- **DM the bot owner to manually re-run binding**: rejected — requires the
  bot owner to be online and aware of every Discord add attempt.

---

## R-FA-01 — Flow A on Discord: deep-link semantics

**Decision**: Discord has no Telegram-equivalent `t.me/<bot>?startgroup=<token>`
URL — the OAuth `state` parameter is invisible to the bot. So Flow A on
Discord uses the same audit-log + manual claim mechanics as the unauthorized-
adder guard:

1. Pending workspace pre-announced with `platform="discord"`,
   `expected_creator_id=<discord user id>`, `expected_platform="discord"`.
2. User adds Robyx to the Discord guild.
3. Audit-log resolves inviter id; if matches `expected_creator_id`, bind.
4. If audit-log fails or returns a different id, the pending workspace
   stays pending; user issues `/im-the-owner <name>` to claim manually.

The `platform` attribute on `[COLLAB_ANNOUNCE]` is a new optional attribute.
Default behavior (no `platform` attribute) is platform-of-the-orchestrator
(i.e. Telegram if announced from HQ on Telegram, or Discord if announced
from a future HQ on Discord — unlikely but supported by the abstraction).

**Rationale**:

- The audit-log mechanic is already required for the unauthorized-adder
  guard. Re-using it for Flow A means one code path, not two.
- `expected_platform` prevents the owner's Telegram user id `42` from
  accidentally binding to a pending workspace meant for the owner's Discord
  user id `42` (the ids are unrelated; collision is plausible).

**Alternatives considered**:

- **Custom OAuth flow with state-passing through a web service**: rejected
  per chat-first principle (II) and operational overhead.
- **Token-in-channel-message**: ask the user to paste a token Robyx
  generated in HQ into the new Discord channel. Rejected — `/im-the-owner`
  is simpler and matches the audit-log fallback shape.

---

## R-LEAVE-01 — `leave_chat` policy for shared guilds

**Decision**: Adapter `leave_chat` stays dumb (calls `guild.leave()`).
Policy lives in `bot/handlers.py:collab_bot_added` (in the
unauthorized-adder guard):

```python
if event.chat_ref.platform == "discord":
    guild_id, _ = event.chat_ref.chat_id.split(":", 1)
    active_in_guild = collab_store.find_active_in_guild(guild_id)
    if any(ws.id != offending_ws_id for ws in active_in_guild):
        log.info("collab.leave_chat skipped: guild %s has %d other active workspaces",
                 guild_id, len(active_in_guild))
        # Skip leave_chat; refusal message posted only in the offending channel.
        return
await platform.leave_chat(event.chat_ref)
```

**Rationale**:

- A Discord guild can legitimately host multiple workspaces (per-channel
  granularity, R-DM-02). `guild.leave()` is a guild-level operation — it
  evicts the bot from every channel in the guild simultaneously. That would
  orphan the legitimate workspaces every time an unauthorized user added the
  bot to a shared guild.
- Keeping the policy in the handler (not the adapter) means the same logic
  applies to Slack in spec 008 (Slack `conversations.leave` is per-channel,
  not per-workspace, but the same shared-channel safety check applies).
- Telegram is unaffected — Telegram chats are 1:1 to workspaces, so
  `find_active_in_guild` returns at most one record and the guard reduces to
  "always leave".

**Alternatives considered**:

- **Per-channel leave on Discord** (kick the bot out of the offending channel
  only): rejected — Discord has no API for this. The bot has guild-wide
  membership; it's either in or out.
- **Refuse to register a new workspace in a guild that already hosts one**:
  rejected — legitimate use case (one guild = one team, multiple project
  channels each with their own collaborative workspace).

---

## R-DM-02 — Discord per-channel granularity

**Decision**: One Discord workspace per channel (not per guild). `chat_id`
encodes `"<guild>:<channel>"`. `find_active_in_guild(guild_id)` returns a
list (potentially > 1).

**Rationale**:

- A guild is a server-wide entity (potentially hundreds of channels,
  multiple teams). Mapping one workspace to one guild would force users to
  choose between collaborative workspaces and the normal guild conversation.
- Per-channel mapping matches Telegram's per-group semantics and Slack's
  per-channel semantics (spec 008). It also matches what users actually
  want: "this channel is where the agent works on Project X".

**Alternatives considered**:

- **Per-guild**: rejected as above.
- **Per-thread (Discord threads, not channels)**: too granular — threads are
  short-lived; collaborative workspaces are long-lived.

---

## R-CMD-01 — Cross-platform mention parser

**Decision**: Rewrite `_parse_user_id(text)` in `bot/handlers.py` to accept:

```
<@123>          → 123        # Discord
<@!123>         → 123        # Discord nickname-aware
<@U12345>       → "U12345"   # Slack (reserved for 008; returns str)
@username       → None       # alphanumeric handle (no resolution)
@123            → 123        # legacy Telegram numeric handle
123             → 123        # bare numeric
```

Implementation: a small dialect table tried in order:

```python
_DISCORD_MENTION = re.compile(r"^<@!?(\d+)>$")
_SLACK_MENTION = re.compile(r"^<@(U[A-Z0-9]+)>$")

def _parse_user_id(text: str) -> int | str | None:
    text = text.strip()
    m = _DISCORD_MENTION.match(text)
    if m:
        return int(m.group(1))
    m = _SLACK_MENTION.match(text)
    if m:
        return m.group(1)
    text = text.lstrip("@")
    try:
        return int(text)
    except ValueError:
        return None
```

**Rationale**:

- Keeps the existing `int | None` return for Telegram callers untouched; new
  `str` return only appears for Slack-style mentions which spec 008 will
  actually consume.
- Discord `<@!id>` (nickname-aware) and `<@id>` (plain) are both standard
  forms emitted by Discord clients; treating them identically is correct.
- Slack `<@U...>` is added now to avoid a second parser refactor in 008.

**Alternatives considered**:

- **Per-platform parser registration**: rejected — over-engineering for 3
  regex patterns.
- **Strict `int`-only return**: rejected — Slack user ids are strings; the
  storage already accepts both via `str(user_id)` keying.

---

## R-MIG-01 — Migration `v0_28_0` strategy

**Decision**: One migration file, idempotent, two-step:

1. Load `data/collaborative_workspaces.json`. For each record:
   - If `"platform"` key is missing: set `platform = "telegram"`.
   - If `chat_id` is `int`: coerce to `str(chat_id)`.
   - If `expected_platform` key is missing: leave it omitted (defaults to
     None on load via `from_dict`). Not strictly needed for back-compat.
2. Atomic write via the same `temp-file + os.replace` primitive used by
   `CollabStore._write_unlocked`.

Idempotency: a successful run leaves every record with `platform: str` and
`chat_id: str`. A second run finds both already set and does nothing.

Partial-failure: atomic write means the file is either old or new — never
mixed. Re-running picks up the unfinished part (which on this migration
means the whole file, since the write is all-or-nothing).

**Rationale**:

- The migration is a pure data transform on a single JSON file. No platform
  side effects, no API calls. Fastest, simplest possible.
- Atomic write reuses an existing well-tested primitive.

**Alternatives considered**:

- **Migration via `CollabStore.load() + save()` round-trip**: rejected —
  introduces a dependency on the in-process `CollabStore` import order
  during the migration runner, complicating the test surface.
- **Lazy migration on first `from_dict` call**: rejected — leaves
  `_chat_map` ambiguity unresolved (`int` vs `str` keys living simultaneously).
  A one-shot rewrite is cleaner.

---

## R-INVITE-01 — Discord invite-link parameters

**Decision**:

```python
import os
DISCORD_INVITE_TTL_DAYS = max(0, int(os.environ.get("DISCORD_INVITE_TTL_DAYS", "7") or "7"))
DISCORD_INVITE_MAX_USES = max(0, int(os.environ.get("DISCORD_INVITE_MAX_USES", "10") or "10"))
```

Both default to `7` and `10` respectively. Invalid env values (negative,
non-int) fall back to defaults with a `log.warning`. `0` means "no limit"
per Discord API conventions (allowed by spec — operator's choice).

Required Discord permission: **`Create Instant Invite`** (documented in FR-021).

**Rationale**: Two env vars cover 95% of operator preferences. Per-workspace
override would add a chat surface (`[COLLAB_ANNOUNCE invite_ttl_days="30"]`)
that nobody has asked for. Keep it minimal; revisit if requested.

---

## Summary of decisions → resolved unknowns

| Unknown | Resolved by |
|---|---|
| Lifecycle abstraction pattern | R-LC-01 |
| Schema extension for cross-platform chat_id | R-DM-01 |
| Per-channel vs per-guild on Discord | R-DM-02 |
| `on_guild_join` no-inviter problem | R-AUTH-01 |
| Manual claim escape hatch | R-AUTH-02 |
| Flow A deep-link replacement on Discord | R-FA-01 |
| `leave_chat` on shared guilds | R-LEAVE-01 |
| Mention syntax across platforms | R-CMD-01 |
| Migration approach | R-MIG-01 |
| Invite-link knobs | R-INVITE-01 |

No open `NEEDS CLARIFICATION` remain. Proceed to Phase 1.
