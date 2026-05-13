# Contract — Lifecycle event dataclasses and Platform callback attributes

**Location**: `bot/messaging/base.py`

## Dataclasses

```python
@dataclass(frozen=True)
class LifecycleAdded:
    chat_ref: ChatRef
    chat_title: str | None = None
    added_by_id: int | str | None = None
    added_by_name: str | None = None
    raw_event: Any = None


@dataclass(frozen=True)
class LifecycleRemoved:
    chat_ref: ChatRef
    chat_title: str | None = None
    raw_event: Any = None


@dataclass(frozen=True)
class LifecycleMigrated:
    old_chat_ref: ChatRef
    new_chat_ref: ChatRef
    raw_event: Any = None
```

## Platform ABC additions

```python
class Platform(abc.ABC):
    # ... existing members ...

    # Lifecycle callbacks — adapters invoke these when their native lifecycle
    # event fires. bot.py registers handlers via these attributes; handlers
    # never branch on platform class.
    on_added:    Callable[[LifecycleAdded],    Awaitable[None]] | None = None
    on_removed:  Callable[[LifecycleRemoved],  Awaitable[None]] | None = None
    on_migrated: Callable[[LifecycleMigrated], Awaitable[None]] | None = None

    @property
    def bot_user_id(self) -> int | str | None:
        """Return the bot's user id on this platform, or None if unavailable.

        - Telegram: int (set after first /getMe call by the adapter).
        - Discord: int (set after on_ready by the adapter).
        - Slack: str like "U01ABC" (set after auth.test by the adapter).
        """
        return None
```

## Dispatch contract

Adapters MUST:

1. Construct the appropriate `LifecycleAdded` / `LifecycleRemoved` /
   `LifecycleMigrated` dataclass from their native event payload.
2. Populate `chat_ref` using the canonical encoding for the platform
   (see contracts/chat-ref.md).
3. If the corresponding callback attribute is set (non-None), `await` it.
4. Tolerate `None` callbacks (no exception, no log spam) — this is the
   default state for adapters that have not been wired into `bot.py` (e.g.
   Slack in spec 007).
5. Catch exceptions raised by the callback and log them with adapter
   context — do NOT let the callback crash the adapter's event loop.

## Telegram-specific dispatch

`bot/messaging/telegram.py` registers a `ChatMemberHandler` internally. The
existing logic currently in `bot/bot.py:_on_my_chat_member` (lines 477-527)
MOVES into the adapter. The adapter constructs `LifecycleAdded` /
`LifecycleRemoved` / `LifecycleMigrated` from `ChatMemberUpdated`:

```python
# Migration:
if new_chat_id:
    await self.on_migrated(LifecycleMigrated(
        old_chat_ref=ChatRef("telegram", str(chat.id)),
        new_chat_ref=ChatRef("telegram", str(new_chat_id)),
        raw_event=member_update,
    ))
elif old_chat_id:
    await self.on_migrated(LifecycleMigrated(
        old_chat_ref=ChatRef("telegram", str(old_chat_id)),
        new_chat_ref=ChatRef("telegram", str(chat.id)),
        raw_event=member_update,
    ))

# Add:
elif new_status in ("member", "administrator") and old_status in ("left", "kicked"):
    await self.on_added(LifecycleAdded(
        chat_ref=ChatRef("telegram", str(chat.id)),
        chat_title=chat.title,
        added_by_id=added_by.id if added_by else None,
        added_by_name=added_by.username if added_by else None,
        raw_event=member_update,
    ))

# Remove:
elif new_status in ("left", "kicked") and old_status in ("member", "administrator"):
    await self.on_removed(LifecycleRemoved(
        chat_ref=ChatRef("telegram", str(chat.id)),
        chat_title=chat.title,
        raw_event=member_update,
    ))
```

## Discord-specific dispatch

`bot/messaging/discord.py` registers `on_guild_join` and `on_guild_remove`
event handlers. The adapter constructs the lifecycle event:

```python
@self._client.event
async def on_guild_join(guild):
    inviter_id, inviter_name = await self._resolve_inviter(guild)
    # Pick the first writable channel as chat_id.
    channel = self._pick_writable_channel(guild)
    if channel is None:
        log.warning("No writable channel in guild %s; leaving", guild.id)
        await guild.leave()
        return
    if self.on_added is not None:
        await self.on_added(LifecycleAdded(
            chat_ref=ChatRef("discord", make_discord_chat_id(guild.id, channel.id)),
            chat_title=guild.name,
            added_by_id=inviter_id,
            added_by_name=inviter_name,
            raw_event=guild,
        ))


@self._client.event
async def on_guild_remove(guild):
    # Close every workspace whose chat_id starts with f"{guild.id}:" — the
    # handler iterates find_active_in_guild and closes them individually.
    if self.on_removed is not None:
        await self.on_removed(LifecycleRemoved(
            chat_ref=ChatRef("discord", make_discord_chat_id(guild.id, 0)),  # channel=0 = sentinel
            chat_title=guild.name,
            raw_event=guild,
        ))
```

The Discord adapter does NOT emit `LifecycleMigrated` — Discord has no
supergroup-migration equivalent. The migration callback remains `None` on
the Discord adapter.

## Slack-specific dispatch (reserved for spec 008)

`Platform.on_added/on_removed` attributes on `bot/messaging/slack.py` remain
`None` in spec 007. Spec 008 will register a `member_joined_channel` handler
that filters for `event["user"] == self.bot_user_id` and emits
`LifecycleAdded` with `added_by_id=event["inviter"]` (string).

## Handler contract

`bot/handlers.py` exposes:

```python
async def collab_bot_added(platform: Platform, event: LifecycleAdded) -> None: ...
async def collab_bot_removed(platform: Platform, event: LifecycleRemoved) -> None: ...
async def collab_bot_migrated(platform: Platform, event: LifecycleMigrated) -> None: ...
```

Branching MUST be on `event.chat_ref.platform`, never on `isinstance(platform, ...)`.
The only platform-aware code paths permitted are:

- `find_active_in_guild` (Discord-specific helper used for `leave_chat` policy).
- `_parse_user_id` dialect table (regex per platform).
- Audit-log lookup invocation (Discord-only; called from the adapter, not the handler).

## Registration in `bot.py`

```python
def _wire_lifecycle(plat, h):
    plat.on_added    = lambda evt: h["collab_bot_added"](plat, evt)
    plat.on_removed  = lambda evt: h["collab_bot_removed"](plat, evt)
    plat.on_migrated = lambda evt: h["collab_bot_migrated"](plat, evt)
```

Called from each platform's bootstrap path (`_run_telegram`, `_run_discord`,
`_run_slack`). Slack registration is a no-op in 007 because the adapter does
not emit lifecycle events yet; the wiring is in place so spec 008 only adds
adapter-side code.
