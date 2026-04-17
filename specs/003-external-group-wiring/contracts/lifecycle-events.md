# Contract: External-Group Lifecycle Events

**Source**: Telegram `my_chat_member` updates wired via `ChatMemberHandler` in `bot/bot.py:461-476`, dispatched to handlers in `bot/handlers.py`.
**Consumer**: `CollabStore` (state transitions) + HQ control-room notifications.

## Event matrix

| Telegram transition | Meaning | Handler | CollabStore call | HQ notification |
|--------------------|---------|---------|------------------|-----------------|
| `left`/`kicked`/`restricted(!member)` → `member`/`administrator` | Bot was just added to a group | `collab_bot_added(platform, chat, added_by)` | Flow A: `update_chat_id`; Flow B: `add` (ad-hoc) | "Collaborative workspace configured" / "I've been added to group …" (existing format, extended with real purpose for Flow B) |
| `member`/`administrator` → `left`/`kicked` | Bot removed from group | `collab_bot_removed(platform, chat)` | `close(ws_id)` | "Collaborative workspace *<name>* has been closed (bot removed from group)" |
| non-`left` → `left` with `migrate_to_chat_id` set | Supergroup migration | `collab_bot_migrated(platform, old_chat_id, new_chat_id)` | `migrate_chat_id(old, new)` | "Workspace *<name>* migrated to new chat_id `<new>`" |
| any → `restricted` with full restrictions | Effectively removed | same as `left` | `close(ws_id)` | same as removed |

## Ordering guarantees

- State transitions persist BEFORE the HQ notification. A crash between persist and notify leaves the registry correct; the operator may miss a notification, which is acceptable (FR-012 logging still records the event).
- On Flow A match, the HQ notification MUST include the pre-announced purpose, not just a generic "bound" message (SC-001).
- On Flow B setup-complete, the HQ notification is emitted by `[COLLAB_SETUP_COMPLETE]`'s handler — NOT by `collab_bot_added`. The initial Flow B add emits a lighter-weight "bot added, setup in progress" notification so HQ knows a setup conversation is underway.

## Authorisation at add-time (FR-011)

`collab_bot_added` calls `authorization.is_authorised_adder(added_by_id)` before Flow A/B branching. Spec for that helper:

```python
def is_authorised_adder(user_id: int) -> bool:
    """True if user is OWNER_ID or has operator/owner role in any existing workspace."""
```

On `False`:
1. `platform.send_message(chat_id=chat_id, text="I can't be added to external groups by this account. Leaving.")`.
2. `platform.leave_chat(chat_id)` (new method on `Platform` ABC; Telegram implements via `bot.leave_chat`; Discord/Slack raise `NotImplementedError` for now — consistent with FR-013 Telegram-only scope).
3. HQ notification: "Unauthorised add attempt to group *<title>* by user `<id>`; left the group."
4. No `CollabWorkspace` is persisted.

## Discord / Slack event contract (FR-013)

When the Discord adapter receives `on_guild_join` or the Slack adapter receives `member_joined_channel` for the bot's user:

1. Send a single message to the new scope: `"External collaborative groups are not yet supported on <Discord|Slack>. Use Telegram for external groups. The bot will take no further action here."`
2. Log `collab.unsupported_platform platform=<name> chat=<id>`.
3. Do NOT persist any `CollabWorkspace`.
4. Do NOT auto-leave (leaving a Discord guild / Slack workspace has stronger side effects than a Telegram group; safer to stay silent).
