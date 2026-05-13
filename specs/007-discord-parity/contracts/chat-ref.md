# Contract — `ChatRef` and platform encodings

**Location**: `bot/messaging/base.py`

## Dataclass

```python
@dataclass(frozen=True)
class ChatRef:
    platform: str
    chat_id: str

    def to_dict(self) -> dict[str, str]:
        return {"platform": self.platform, "chat_id": self.chat_id}

    @classmethod
    def from_dict(cls, d: dict) -> "ChatRef":
        return cls(platform=d["platform"], chat_id=str(d["chat_id"]))
```

## Invariants

- `platform` MUST be one of `{"telegram", "discord", "slack"}`. Unknown values
  are accepted by the dataclass constructor (forward-compat) but produce a
  `log.warning` from the lifecycle dispatcher (`bot/handlers.py`).
- `chat_id` MUST be a non-empty string. The dataclass does not validate the
  encoding (per-platform validation lives in helper functions below) but
  callers MUST emit canonical form.
- `ChatRef` is **frozen** — attempting to mutate raises `FrozenInstanceError`.
- `ChatRef` is **hashable** via the frozen-dataclass default; can be used as
  dict key (`_chat_map` keys by `(platform, chat_id)` tuples, not `ChatRef`
  itself, to keep parity with pre-007 internal map shape).

## Canonical chat_id encodings

| Platform | Form | Regex (informative) | Example |
|---|---|---|---|
| `telegram` | `"<chat_id_int>"` | `^-?\d+$` | `"-1001234567890"` |
| `discord` | `"<guild>:<channel>"` | `^\d+:\d+$` | `"123456789012345678:987654321098765432"` |
| `slack` | `"<team>:<channel>"` | `^T[A-Z0-9]+:[A-Z][A-Z0-9]+$` (reserved for 008) | `"T01ABC:C02DEF"` |

## Helpers (in `bot/collaborative.py`)

```python
def parse_discord_chat_id(chat_id: str) -> tuple[int, int]:
    """Split a Discord chat_id into (guild_id, channel_id) ints.

    Raises ValueError on malformed input.
    """
    parts = chat_id.split(":", 1)
    if len(parts) != 2:
        raise ValueError("malformed Discord chat_id: %r" % chat_id)
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as e:
        raise ValueError("non-integer Discord chat_id components: %r (%s)" % (chat_id, e))


def make_discord_chat_id(guild_id: int, channel_id: int) -> str:
    return "%d:%d" % (guild_id, channel_id)
```

The Slack helpers (`parse_slack_chat_id`, `make_slack_chat_id`) are introduced
by spec 008.

## Usage

Lifecycle dispatch (in `bot/messaging/discord.py`):

```python
chat_ref = ChatRef(
    platform="discord",
    chat_id=make_discord_chat_id(guild.id, channel.id),
)
event = LifecycleAdded(
    chat_ref=chat_ref,
    chat_title=guild.name,
    added_by_id=inviter_id,
    added_by_name=inviter_name,
    raw_event=guild,
)
if self.on_added is not None:
    await self.on_added(event)
```

Handler dispatch (in `bot/handlers.py`):

```python
async def collab_bot_added(platform: Platform, event: LifecycleAdded) -> None:
    chat_id = event.chat_ref.chat_id
    plat = event.chat_ref.platform
    # ... branch on `plat` only where strictly required (find_active_in_guild,
    # _parse_user_id dialect, leave_chat policy). Never on isinstance(platform).
```

## Backwards compatibility

Pre-007 lifecycle handlers had signatures like:

```python
async def collab_bot_added(platform, chat, added_by): ...
async def collab_bot_removed(platform, chat): ...
async def collab_bot_migrated(platform, old_chat_id, new_chat_id): ...
```

Spec 007 changes these to:

```python
async def collab_bot_added(platform, event: LifecycleAdded): ...
async def collab_bot_removed(platform, event: LifecycleRemoved): ...
async def collab_bot_migrated(platform, event: LifecycleMigrated): ...
```

Tests that previously constructed `chat` and `added_by` mock objects directly
are updated to construct `LifecycleAdded(chat_ref=..., chat_title=..., added_by_id=...)`
in Phase 3 (T021–T025).
