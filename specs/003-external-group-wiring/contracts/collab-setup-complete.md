# Contract: `[COLLAB_SETUP_COMPLETE ...]`

**Emitter**: a collaborative-workspace agent, while its backing `CollabWorkspace` is in `status="setup"`.
**Consumer**: `bot/handlers.py::_handle_collab_setup_complete` (invoked from `_process_and_send` for collaborative agents).
**Purpose**: Signal that the AI-driven setup conversation has captured enough to finalise the workspace.

## Grammar

```
[COLLAB_SETUP_COMPLETE purpose="<text>" inherit="<slug-or-empty>" inherit_memory="true|false"]
```

All attributes are required. Same quoting convention as `[COLLAB_ANNOUNCE]`.

## Attribute rules

| Attribute | Type | Constraints |
|-----------|------|-------------|
| `purpose` | `str` | 1–512 chars; replaces the seed boilerplate in `data/agents/<name>.md`. |
| `inherit` | `str` | Either empty or an existing workspace name. |
| `inherit_memory` | `bool` | Literal `"true"` or `"false"`. |

## Handler semantics (ordering matters — matches existing race-closing pattern)

1. Resolve `collab_ws` by agent name. If `collab_ws.status != "setup"`: log WARNING, strip marker, return.
2. Rewrite `data/agents/<name>.md` with `# <display_name>\n\n<purpose>\n\n(Inherits from: <inherit>; memory inherit: <inherit_memory>)\n`.
3. On OSError: log ERROR, leave status as `"setup"`, send a recoverable failure message in the group ("Couldn't save setup — please retry"), and strip the marker. **Do not** flip status.
4. On success: call `collab_store.finalize_setup(ws_id, parent_workspace=..., inherit_memory=...)`.
5. Post the real HQ notification:

   ```
   *Collaborative workspace setup complete*

   Workspace *<display>* (`<name>`) is now active.
   Purpose: <purpose>
   Inherits: <inherit or "none"> (memory: <inherit_memory>)
   chat_id: <chat_id>
   ```

6. Strip the marker from the response before sending to the group. The group sees the agent's natural-language conclusion (e.g., "Great — we're set up. Ask me anything about Nebula.") without the raw command.

## Failure modes

- Invalid status (already `active` / `closed`): marker ignored, log WARNING, no HQ noise.
- Disk write failure: agent stays in setup; user can retry.
- `inherit` references a non-existent workspace: proceed but log WARNING; `parent_workspace` still set (same posture as `create_pending`).
