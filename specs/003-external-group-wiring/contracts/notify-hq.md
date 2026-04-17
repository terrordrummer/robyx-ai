# Contract: `[NOTIFY_HQ ...]`

**Emitter**: any collaborative-workspace agent (status `active` or `setup`).
**Consumer**: `bot/handlers.py` command parser, invoked via `_process_and_send` for collaborative agents.
**Purpose**: Let an external-group agent surface a short update to HQ without going through the orchestrator.

## Grammar

```
[NOTIFY_HQ text="<message>"]
```

## Attribute rules

| Attribute | Type | Constraints |
|-----------|------|-------------|
| `text` | `str` | 1–2000 chars. Longer is truncated with ellipsis; truncation is logged. |

## Handler semantics

1. Resolve the source group from the invocation context (`collab_ws` of the handler).
2. Compose the HQ message:

   ```
   *[<display_name>]* (`<name>`): <text>
   ```

3. Deliver to `CHAT_ID` on `platform.control_room_id` via `platform.send_message(...)`.
4. Strip the marker from the group-facing response so the group sees only the agent's natural-language turn.

## Failure modes

- Delivery failure: log WARNING, do not surface to the group (the agent can retry on a subsequent turn).
- Unknown source group (defensive — should not happen): log ERROR, drop the marker.

## Security notes

- `[NOTIFY_HQ]` is stripped by the outgoing executive-marker filter at `handlers.py:424-425` when the originating user message was non-executive (`is_executive=False`). This is intentional: a participant's prompt injection cannot cause the agent to leak arbitrary content to HQ. Executive users (owner/operator in the group) can trigger legitimate notifications.

## Example

Nebula group, executive message from Alice: "Let HQ know we landed on plan B."

Agent response (pre-strip):

```
Will do.

[NOTIFY_HQ text="Alice confirmed: we're going with plan B for the Nebula analysis."]
```

Delivered to HQ control room:

```
*[Nebula Research]* (`nebula`): Alice confirmed: we're going with plan B for the Nebula analysis.
```
