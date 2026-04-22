# Contract — `[GET_EVENTS]` Macro

## Grammar

```
[GET_EVENTS since="<duration_or_iso>" task="<task_name>?" type="<event_type>?" limit="<int>?"]
```

Expressed as a regex (mirroring `NOTIFY_HQ_PATTERN` in `bot/ai_invoke.py`):

```python
GET_EVENTS_PATTERN = re.compile(
    r'\[GET_EVENTS\s+([^\]]+?)\s*\]',
    re.DOTALL,
)
```

Attribute parser: reuse `_COLLAB_ATTR_PATTERN` (exists).

## Attributes

| Attr | Required | Format | Semantics |
|---|---|---|---|
| `since` | YES | Duration (`30m`, `2h`, `1d`, `3600s`) OR ISO-8601 UTC (`2026-04-22T12:00:00Z`) | Lower bound of the time window |
| `task` | no | Task safe_name | Exact-match filter. Absent = all tasks |
| `type` | no | One event_type string from the taxonomy | Exact-match filter. Absent = all types |
| `limit` | no | Integer 1–1000 | Caps returned entries (newest first); default 200 |

Malformed attributes (bad duration, unknown task, non-integer limit) → return a structured error (see § Error Injection) rather than a silent partial.

## Handler flow (`bot/handlers.py` — new `_handle_get_events`)

1. Intercept pattern in `ai_invoke.MACRO_PATTERNS` pre-response-send (parallel to `NOTIFY_HQ`).
2. **Strip** every `[GET_EVENTS ...]` token from the outgoing `response` text.
3. Parse attributes. On malformed input → emit an error system message back into the orchestrator's context for the same turn (so the agent can self-correct). Do not send anything to the user.
4. Compute query window from `since`. Call `bot.events.query(...)`.
5. Serialise the result into a compact markdown table and inject it back as a system-role context message:
   ```
   <system>
   Events since 2026-04-22T12:00:00Z (14 entries):

   | ts                          | task          | type            | outcome          |
   |-----------------------------|---------------|-----------------|------------------|
   | 2026-04-22T12:03:15.123456Z | zeus-research | dispatched      | step_17          |
   | 2026-04-22T12:18:42.998001Z | zeus-research | step_complete   | awaiting_input   |
   | ...                         | ...           | ...             | ...              |

   Payload snippets available on request via `[GET_EVENTS … limit="…"]` with task-specific filters.
   </system>
   ```
6. Agent uses the injected context to compose a user-facing narrative on the same turn. The raw table is NOT visible to the user unless the agent echoes it back.
7. Log invocation at INFO: `handlers.get_events ws=<agent_name> since=<…> task=<…> type=<…> returned=<N>`.

## Error injection format

```
<system>
[GET_EVENTS] error: <machine-readable code>: <human message>

Attributes received: since=<…> task=<…> type=<…> limit=<…>
</system>
```

Error codes:
- `INVALID_DURATION` — `since` could not be parsed
- `UNKNOWN_TASK` — `task=<X>` not found in state registry (warn-level; still returns results from journal since task may have been deleted)
- `INVALID_LIMIT` — `limit` outside [1, 1000]
- `WINDOW_TOO_LARGE` — `since` older than retention window; results are clamped and a note is included in the output

## Access control

- Must be invoked by an LLM-authored message in an authenticated workspace (same existing contract as `[NOTIFY_HQ]` / `[GET_PLAN]`).
- No user-typed `[GET_EVENTS ...]` is interpreted (same escape semantics as other macros — a user pasting the literal string is ignored by the handler).

## Idempotency

The macro is read-only. Repeated emission in the same turn is allowed; each execution is independent and returns fresh data.

## Non-goals

- Full-text search over payloads (queries are by ts/task/type only).
- Streaming / subscription (out of scope).
- Aggregation (`count`, `group by`) — the agent can synthesise from the raw list.
