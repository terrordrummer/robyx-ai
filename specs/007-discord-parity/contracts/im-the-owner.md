# Contract — `/im-the-owner <workspace-name>` command

**Location**: `bot/handlers.py` (command handler), exposed through the existing
`on_message` dispatch on Discord. Reserved for Slack in spec 008.

## Purpose

Manual escape hatch for the Discord audit-log lookup failure case
(`contracts/discord-audit-log.md`). Allows a user to explicitly claim a
pending collaborative workspace by typing the command in any channel of the
guild they intend to bind.

## Grammar

```
/im-the-owner <workspace-name>
```

- Single positional argument: the workspace `name` (canonical form
  `[a-z0-9][a-z0-9-]{0,63}` per `validate_collab_name`).
- No flags, no quoting, no multi-word argument.
- Case-sensitive (matches the canonical name).
- Whitespace before/after the argument is trimmed.

## Preconditions (all must hold)

| Check | Refusal STRING | Effect |
|---|---|---|
| Workspace `<name>` exists in `CollabStore` | `im_the_owner_unknown_workspace` | refuse |
| `ws.status == "pending"` | `im_the_owner_already_bound` | refuse |
| `ws.platform == "discord"` | `im_the_owner_platform_mismatch` | refuse |
| `ws.expected_platform in (None, "discord")` | `im_the_owner_platform_mismatch` | refuse |
| `ws.expected_creator_id == msg.user_id` (Discord user id) | `im_the_owner_creator_mismatch` | refuse |

If all checks pass:

1. Bind `ws.chat_id = make_discord_chat_id(guild_id, channel_id)` where
   `channel_id` is the channel of the `/im-the-owner` message.
2. Promote `ws.status = "active"` and set `ws.expected_platform = None`.
3. Run the same downstream flow as the audit-log success path:
   - Set role: `ws.set_role(user_id, Role.OWNER)`.
   - Generate invite link via `platform.get_invite_link(chat_ref)`.
   - Post `STRINGS["collab_welcome_pending"] % (display_name, purpose)` in
     the binding channel.
   - Notify HQ via `STRINGS["collab_bot_added_hq_matched"] % (display_name, ...)`.

## Refusal messages (i18n STRINGS — new keys)

```python
"im_the_owner_unknown_workspace": (
    "I don't know about a pending workspace named *%s*. List pending "
    "workspaces from HQ to confirm the name."
),
"im_the_owner_already_bound": (
    "Workspace *%s* is already bound (`%s`). Manual claim is only allowed for "
    "pending workspaces."
),
"im_the_owner_platform_mismatch": (
    "Workspace *%s* was pre-announced for *%s*, not Discord. Manual claim "
    "is refused."
),
"im_the_owner_creator_mismatch": (
    "Workspace *%s* was pre-announced for a different user. Ask the original "
    "announcer to claim it, or recreate the workspace pinned to your Discord id."
),
"im_the_owner_no_pending": (
    "There are no pending workspaces awaiting a manual claim on Discord. "
    "Pre-announce one from HQ with `[COLLAB_ANNOUNCE name=\"...\" platform=\"discord\"]` first."
),
"im_the_owner_success": (
    "Workspace *%s* is now active in this channel. HQ has been notified."
),
```

## Handler skeleton

```python
async def _handle_im_the_owner(platform, msg, msg_ref) -> None:
    """Manual claim of a pending Discord workspace. See contracts/im-the-owner.md."""
    if collab_store is None:
        return
    # Only Discord supports manual claim in 007 (Slack will follow in 008).
    # We do NOT branch on platform class — we branch on the message's chat_ref.
    parts = (msg.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await platform.reply(msg_ref, "Usage: `/im-the-owner <workspace-name>`",
                             parse_mode="markdown")
        return
    name = parts[1].strip()
    # Lookup
    ws = next((w for w in collab_store.list_all() if w.name == name), None)
    if ws is None:
        await platform.reply(msg_ref, STRINGS["im_the_owner_unknown_workspace"] % name,
                             parse_mode="markdown")
        return
    if ws.status != "pending":
        await platform.reply(msg_ref, STRINGS["im_the_owner_already_bound"] % (name, ws.status),
                             parse_mode="markdown")
        return
    # In 007 we only support Discord claims; in 008 the same handler will
    # extend to Slack by checking msg.chat_ref.platform.
    if ws.platform != "discord" or (
        ws.expected_platform is not None and ws.expected_platform != "discord"
    ):
        await platform.reply(msg_ref,
            STRINGS["im_the_owner_platform_mismatch"] % (name, ws.expected_platform or ws.platform),
            parse_mode="markdown")
        return
    if ws.expected_creator_id is not None and ws.expected_creator_id != msg.user_id:
        await platform.reply(msg_ref, STRINGS["im_the_owner_creator_mismatch"] % name,
                             parse_mode="markdown")
        return
    # Bind. msg.chat_id on Discord is "<guild>:<channel>" already.
    chat_ref = ChatRef(platform="discord", chat_id=msg.chat_id)
    if not collab_store.update_chat_id(ws.id, chat_ref, expected_creator_id=msg.user_id):
        log.warning("im_the_owner: update_chat_id refused for %s", ws.id)
        return
    collab_store.update_roles(ws.id, msg.user_id, Role.OWNER)
    # Invite + welcome + HQ notify — same as audit-log success path.
    # (Implementation reuses the helper extracted in Phase 4 task T028.)
    await _flow_a_post_bind(platform, ws, chat_ref)
    await platform.reply(msg_ref, STRINGS["im_the_owner_success"] % ws.display_name,
                         parse_mode="markdown")
```

## Test coverage

- `tests/test_collab_im_the_owner.py::test_success_path` — pending ws matching
  creator + platform → bound + active + welcome posted.
- `test_unknown_workspace` — golden error STRING surfaces.
- `test_already_bound` — workspace in `active`/`closed` status → golden error.
- `test_platform_mismatch_telegram` — pending ws has `platform="telegram"`;
  command refused.
- `test_creator_mismatch` — message from a user other than `expected_creator_id`;
  command refused.
- `test_usage_error` — bare `/im-the-owner` (no arg) → usage message.

## Observability

- INFO log on success: `collab.discord.manual_claim ws_id=%s by=%s chat=%s`.
- WARN log on each refusal: `collab.discord.manual_claim.refused reason=%s ws=%s by=%s`.
