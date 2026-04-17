# Contract: `[COLLAB_SEND ...]`

**Emitter**: orchestrator agent (`robyx`) only.
**Consumer**: `bot/handlers.py` command parser (added to `_handle_workspace_commands` branch called from `_process_and_send`).
**Purpose**: Let the orchestrator deliver a message into a specific external group.

## Grammar

```
[COLLAB_SEND name="<slug>" text="<message>"]
```

Both attributes required. `text` may contain newlines (encoded as literal `\n` or preserved — parser must tolerate multiline quoted content, matching `[DELEGATE]` behaviour).

## Attribute rules

| Attribute | Type | Constraints |
|-----------|------|-------------|
| `name` | `str` | Must match a `CollabWorkspace.agent_name` with `status=="active"`. |
| `text` | `str` | 1–4000 chars (Telegram single-message cap); longer is split by the platform adapter's existing splitter. |

## Handler semantics

1. Authorise: only when invoked from an orchestrator response (`agent.name == "robyx"` at the `_process_and_send` call site). Reject otherwise with `[COLLAB_SEND rejected: only orchestrator may send]`.
2. Resolve `ws = collab_store.get_by_agent_name(name)`:
   - `ws is None`: append `[COLLAB_SEND error: unknown group <name>]`.
   - `ws.status != "active"`: append `[COLLAB_SEND error: group <name> not active (status=<status>)]`.
3. Otherwise:
   - `await platform.send_message(chat_id=ws.chat_id, text=text, thread_id=None)`.
   - Append `[COLLAB_SEND ok: <name>]`.
4. Strip the original marker from the response before sending to HQ.

## Failure modes tested

- Unknown group: `error: unknown group <name>`.
- Closed group (still in store but closed): `error: group <name> not active`.
- Platform delivery failure (raised exception): `error: delivery failed: <exc>`; log ERROR.

## Example

Roberto in HQ: "Tell the Nebula group we'll skip tomorrow's session."

Orchestrator response (pre-strip):

```
Sent.

[COLLAB_SEND name="nebula" text="Heads-up: we're skipping tomorrow's session."]
```

Post-parse response to Roberto:

```
Sent.

[COLLAB_SEND ok: nebula]
```

And in the Nebula group:

```
Heads-up: we're skipping tomorrow's session.
```
