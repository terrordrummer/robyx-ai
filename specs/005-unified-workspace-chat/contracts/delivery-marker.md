# Contract — Delivery Marker

## Purpose

Every outbound message produced by the scheduler on behalf of a task MUST carry a consistent visual marker identifying its type and name. The marker is applied at a single delivery chokepoint so that agent output, reminder text, and error messages are all uniformly marked.

## Scope

Applies to:
- Continuous task step deliveries (output from secondary agent via `deliver_task_output`)
- Periodic task deliveries (scheduled agent output)
- One-shot task deliveries (scheduled agent output)
- Reminder deliveries (plain text, no LLM, dispatched by `_dispatch_reminders`)

Does NOT apply to:
- Primary agent's conversational responses (interactive path)
- System control messages emitted by the bot outside of scheduled delivery
- User-authored inbound messages (even if they contain icon-like characters)

## Format

```text
<icon> [<task-name>] <body>
```

- Exactly one space between icon and `[`.
- Exactly one space between `]` and the body.
- `<body>` is the sanitized output (macros already stripped, `[STATUS …]` removed, `[SILENT]` honoured).
- When `<body>` is multi-paragraph, the marker precedes the first line; subsequent lines are not re-prefixed.
- When the body is empty and `returncode == 0`, the existing "Task completed, but it did not produce any visible output" message is used; the marker still prefixes it.
- Error messages retain their existing formatting; the marker prefixes the full error block.

## Icon map

| Task type (from `queue.json::type`) | Icon |
|-------------------------------------|------|
| `continuous`                         | 🔄   |
| `periodic`                           | ⏰   |
| `one-shot` (aliases: `oneshot`, `one_shot`) | 📌 |
| `reminder`                           | 🔔   |

Unknown task types MUST fall back to no marker (do not invent an icon) and MUST log a WARN including the unknown type string. A fallback delivery without a marker is still delivered — markers are a UX aid, not a correctness gate.

## Implementation chokepoint

Single helper `format_delivery_message(task_type: str, task_name: str, body: str) -> str` in `bot/scheduled_delivery.py`. Consumers:
- `bot/scheduled_delivery.py::_render_result_message()` — agent task outputs
- `bot/scheduler.py::_dispatch_reminders()` — reminder sends (wrap `reminder["message"]` before `platform.send_message`)

No other code path is permitted to format outbound scheduled messages.

## Idempotency

Calling `format_delivery_message` twice on an already-formatted body is NOT supported; the helper does not detect pre-existing markers. Callers MUST apply it exactly once. This is enforced by the single-chokepoint rule above and covered by tests.

## Length & truncation

Platform adapters apply their own `split_message()` after the marker is added. Long task names (>64 chars) are truncated to 64 chars with a trailing `…` by `format_delivery_message` to keep the marker visually bounded; the full name appears unchanged elsewhere (state.json, logs, list command).

## Test assertions

- Given a `type=continuous` task with `name="daily-report"` and body `"Step 3 completato"`, the formatted message starts with `"🔄 [daily-report] "`.
- Given a `type=reminder` with `message="Check CI"`, the dispatched message starts with `"🔔 [<reminder-id-or-name>] "`.
- Given an unknown type string, the formatted message equals the raw body (no marker) AND a WARN is logged.
- Given a conversational (interactive) response, NO marker is prepended.
