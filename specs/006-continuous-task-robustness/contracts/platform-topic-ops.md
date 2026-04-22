# Contract — Platform Adapter Topic Operations

Extends the `Platform` ABC in `bot/messaging/base.py` with five new methods. All adapters MUST implement all methods; those lacking platform primitives MUST implement no-op fallbacks with a `WARN` log on first call per session (per Constitution Principle I — documented degradation).

## New ABC methods

```python
# bot/messaging/base.py

async def edit_topic_title(
    self,
    channel_id: int,
    new_title: str,
) -> bool
    """Update the display title of a topic/channel. Returns True on success,
    False on platform error. Raises TopicUnreachable if the topic has been
    deleted externally by a user."""


async def pin_message(
    self,
    chat_id: int,
    thread_id: int,
    message_id: int,
) -> bool
    """Pin a message in a specific topic. Returns True on success. Silent
    no-op + WARN log on platforms without per-topic pinning."""


async def unpin_message(
    self,
    chat_id: int,
    thread_id: int,
    message_id: int | None = None,
) -> bool
    """Unpin a specific message, or all pins in the topic if message_id is
    None. Returns True on success. No-op + WARN on unsupporting platforms."""


async def close_topic(
    self,
    channel_id: int,
) -> bool
    """Close a topic to new messages (read-only). History remains visible.
    Returns True on success."""


async def archive_topic(
    self,
    channel_id: int,
    display_name: str,
) -> bool
    """Composite: edit_topic_title to '[Archived] <display_name>' + close_topic.
    Atomic-ish (if rename succeeds but close fails, caller sees False and can
    retry close; idempotent). Returns True only if both succeeded."""
```

## Platform-specific implementations

### Telegram (`bot/messaging/telegram.py`)

| ABC method | Bot API call |
|---|---|
| `edit_topic_title` | `editForumTopic` with `name` only |
| `pin_message` | `pinChatMessage` with `chat_id`, `message_id`, `disable_notification=True` (silent) |
| `unpin_message` | `unpinChatMessage` (single) or `unpinAllForumTopicMessages` (bulk, for thread) |
| `close_topic` | `closeForumTopic` |
| `archive_topic` | compose: `editForumTopic(name="[Archived] X")` → `closeForumTopic` |

**TopicUnreachable mapping**: Bot API error `Bad Request: TOPIC_ID_INVALID` or `TOPIC_CLOSED` (for mutating operations) → raise `TopicUnreachable(channel_id)`.

### Discord (`bot/messaging/discord.py`)

| ABC method | discord.py call |
|---|---|
| `edit_topic_title` | `channel.edit(name=new_title)` (requires `manage_channels`) |
| `pin_message` | `(await channel.fetch_message(message_id)).pin()` |
| `unpin_message` | `(await channel.fetch_message(message_id)).unpin()` or `for m in await channel.pins(): await m.unpin()` |
| `close_topic` | For threads: `thread.edit(archived=True, locked=True)`; for channels: best-effort `channel.set_permissions(role=everyone, send_messages=False)` + WARN log |
| `archive_topic` | `thread.edit(name="[Archived] X", archived=True, locked=True)` |

### Slack (`bot/messaging/slack.py`)

| ABC method | Slack API call |
|---|---|
| `edit_topic_title` | `conversations.rename(channel=..., name=...)` (requires `channels:manage` + name format rules — may need sanitisation) |
| `pin_message` | `pins.add(channel=..., timestamp=message_ts)` (pins are workspace-wide) |
| `unpin_message` | `pins.remove(channel=..., timestamp=message_ts)` |
| `close_topic` | `conversations.archive(channel=...)` (permanent-ish; requires admin to unarchive) — WARN log on first call explaining this |
| `archive_topic` | `conversations.rename(name="archived-X")` + `conversations.archive(channel=...)` (SC: channel name slug rules apply) |

**Slack caveats logged once per session** (for operator awareness):
- "Slack pins are workspace-wide, not topic-scoped — user may see pins from other tasks in the pinned-items view."
- "Slack archived channels disappear from most UIs — use sparingly."

## Error types

```python
# bot/messaging/base.py

class TopicUnreachable(Exception):
    """Raised when a topic/channel has been deleted or is otherwise
    permanently inaccessible. Callers (scheduler, delivery) MUST catch
    this and invoke the FR-002a last-resort HQ surface path."""
    def __init__(self, channel_id: int, reason: str = ""):
        self.channel_id = channel_id
        self.reason = reason
        super().__init__("Topic %s unreachable: %s" % (channel_id, reason))
```

Transient errors (rate-limits, network hiccups) MUST be retried up to 3 times with exponential backoff (0.5 s, 1 s, 2 s) before surfacing as a regular failure (return `False`). Only permanent unreachability raises `TopicUnreachable`.

## Concurrency

- All methods are `async` and MUST be safe to call concurrently on the same adapter instance (the existing adapter pattern already uses a shared `httpx.AsyncClient`).
- Multiple `pin_message` calls on the same topic race: last-write-wins at the platform level; we maintain our own `awaiting_pinned_msg_id` as the authoritative record and unpin-before-pin when replacing.

## Testing parity

Each new method MUST have a test in `tests/test_platform_topic_ops.py` exercising all three adapters with:
- Happy path (call succeeds)
- `TopicUnreachable` path (simulated 404 / `TOPIC_ID_INVALID`)
- Transient-error retry (simulated rate-limit + success on retry)
- No-op platforms (Slack close_topic) log WARN exactly once per session
