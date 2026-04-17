# Contract: `[COLLAB_ANNOUNCE ...]`

**Emitter**: orchestrator agent (`robyx`) only, in HQ.
**Consumer**: `bot/handlers.py` command parser (alongside `[DELEGATE]`, `[FOCUS]`).
**Purpose**: Pre-announce an external collaborative group before the bot is added.

## Grammar

```
[COLLAB_ANNOUNCE name="<slug>" display="<text>" purpose="<text>" inherit="<slug-or-empty>" inherit_memory="true|false"]
```

All attributes are required. Attribute order is free. Double-quoted string values MUST be used (consistent with `[DELEGATE @name: text]` but attribute-style, matching the "tool-ish" payload shape the agent already produces for `[SEND_IMAGE path="..."]`).

## Attribute rules

| Attribute | Type | Constraints |
|-----------|------|-------------|
| `name` | `str` | 3–32 chars; `[a-z0-9-]+`; must not collide with any existing `CollabWorkspace.name` or `RESERVED_AGENT_NAMES` (`bot/topics.py:27`). |
| `display` | `str` | 1–128 chars; shown to humans; may include unicode. |
| `purpose` | `str` | 1–512 chars; captured into the agent's seed `.md` file. |
| `inherit` | `str` | Either empty (`""`) for "start fresh", or the name of an existing agent/workspace. Non-existent target logs WARNING but does not fail. |
| `inherit_memory` | `bool` | Literal `"true"` or `"false"`. Defaults to `"true"` if missing (though it is required in v1). |

## Parser semantics

1. On match:
   - Authorise: the current chat MUST be HQ (`platform.is_main_thread(...)`) AND `msg.user_id == OWNER_ID`. Reject otherwise with a single error line appended to the orchestrator's response: `[COLLAB_ANNOUNCE rejected: not authorised]`.
   - Call `collab_store.create_pending(...)`.
   - On success: append `[COLLAB_ANNOUNCE ok: name=<name>]` to the orchestrator response so the user sees confirmation.
   - On failure (name collision, ValueError, OSError): append `[COLLAB_ANNOUNCE error: <reason>]`.
2. Strip the original marker from the response before delivery.

## Example (orchestrator output)

Input from Roberto in HQ:

> "I'm going to create a Telegram group with Alice and Bob for the Nebula research project. Use our astro-research workspace as a base."

Orchestrator response (pre-strip):

```
Got it — I've prepared a workspace called "nebula" inheriting from astro-research. Add me to the group when you're ready.

[COLLAB_ANNOUNCE name="nebula" display="Nebula Research" purpose="Collaboration with Alice and Bob on Nebula research; inherits astro-research skills and memory" inherit="astro-research" inherit_memory="true"]
```

After parser strips and confirms:

```
Got it — I've prepared a workspace called "nebula" inheriting from astro-research. Add me to the group when you're ready.

[COLLAB_ANNOUNCE ok: name=nebula]
```

## Failure modes tested

- Collision: `[COLLAB_ANNOUNCE error: name collision "nebula"]`.
- Not authorised: `[COLLAB_ANNOUNCE rejected: not authorised]`.
- Malformed attributes: parser skips silently and logs WARNING (same failure posture as malformed `[DELEGATE]`).
