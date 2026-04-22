# Contract — Structured Delivery Header

Every continuous-task delivery (step complete, awaiting-input pin, reminder, stop/complete/delete notice, orphan incident) MUST prefix its body with a single-line structured header.

## Grammar

```
<icon> [<task_name>] · Step <step_counter> · <state_emoji> <state_label> · <HH:MM>
```

Optionally followed by a second-line continuation:

```
→ Next: <next_step_preview_truncated_to_80_chars>
```

Then a blank line, then the body.

## Regex (for tests and downstream parsers)

```python
DELIVERY_HEADER_RE = re.compile(
    r'^'
    r'(?P<icon>\S+)\s+'
    r'\[(?P<name>[a-z0-9][a-z0-9-]{0,63})\]\s+·\s+'
    r'Step\s+(?P<step>\d+(?:/\d+)?)\s+·\s+'
    r'(?P<state_emoji>\S+)\s+(?P<state_label>[^·]+?)\s+·\s+'
    r'(?P<hhmm>\d{2}:\d{2})'
    r'$'
)
```

## Field semantics

| Field | Source | Format |
|---|---|---|
| `icon` | `TASK_TYPE_ICONS[task_type]` | `🔄` for continuous, `⏰` periodic, `📌` one-shot, `🔔` reminder |
| `name` | `task.name` (safe_name) | Matches `^[a-z0-9][a-z0-9-]{0,63}$` |
| `step` | `N` or `N/M` | `N` from `current_step.number`; `M` = `program.total_steps` if set, else omitted |
| `state_emoji` + `state_label` | Derived from `task.status` | See mapping below |
| `hhmm` | `datetime.now(user_tz).strftime("%H:%M")` | User's configured timezone |

## State mapping

| `task.status` | `state_emoji` | `state_label` |
|---|---|---|
| `running` | `▶` | `running` |
| `awaiting_input` | `⏸` | `awaiting input` |
| `rate_limited` | `⏳` | `rate-limited until HH:MM` (`HH:MM` from `rate_limited_until`) |
| `stopped` | `⏹` | `stopped` |
| `completed` | `✅` | `completed` |
| `error` | `❌` | `error` |
| Special: workspace closed mid-step | `⚠` | `workspace closed` |
| Special: drain timeout | `⏱` | `drain timeout` |

## Next-step preview

When `next_step.description` is set AND the delivery is not itself terminal (not `completed` / `error` / `deleted`):

- Truncate to 80 characters.
- If truncated, append `…`.
- Render as: `→ Next: <truncated_description>`
- Place as the second line, immediately before the blank line and body.

## Examples

**Step complete, going to awaiting-input:**
```
🔄 [zeus-research] · Step 12 · ⏸ awaiting input · 14:31
→ Next: awaits user decision on multi-reference-C vs calibration-scale-narrowing

Step 12 complete. Two topics closed, one decision needed…
```

**Normal in-progress step complete, continuing:**
```
🔄 [zeus-rd-172] · Step 17/30 · ▶ running · 06:48
→ Next: iter-189 T-A iter-001 — opening diagnostic on A clock_hands blending_seam

iter-188 T-B iter-008 σ-sweep kill-switch — AUTO-REVERTED on every σ…
```

**Rate-limited transition:**
```
🔄 [zeus-research] · Step 3 · ⏳ rate-limited until 15:42 · 14:42
```
(no body necessary for state-transition deliveries)

**Orphan incident escalation:**
```
🔄 [zeus-engine] · Step 7 · ❌ error · 03:15

Orphan incident: task has failed to heartbeat for 3 consecutive scheduler cycles.
Last exit code: -9 (SIGKILL). Last output tail: …

Diagnostic payload journaled. Task will not auto-recover; use [RESUME_TASK] to retry or [DELETE_TASK] to archive.
```

**Workspace closed mid-step:**
```
🔄 [zeus-research] · Step 5 · ⚠ workspace closed · 17:05

Drain delivered final output after parent workspace was closed…
```

**Delete notification (final message before archive):**
```
🔄 [zeus-research] · Step 12 · ✅ completed · 18:00

Task deleted and topic archived as [Archived] zeus-research. Name is now free for reuse.
Query [GET_EVENTS task="zeus-research"] to inspect the full lifecycle history.
```

## Header placement rules

- Always first line of the message.
- Always followed by exactly one blank line before any `→ Next` second line or the body.
- `→ Next` line, if present, is followed by exactly one blank line before the body.
- Body is whatever the agent produced, after `SILENT`-pattern stripping and control-token removal (unchanged from today).

## Single chokepoint

The header is computed and prepended in `scheduled_delivery._render_result_message`. Agents MUST NOT attempt to format a header themselves. The delivery layer is the single authority (same contract as spec 005 for the type-icon marker).

If an agent's output already begins with something that looks like a header (regex match), the renderer MUST strip it before prepending the canonical header. Defensive: we cannot trust the agent to maintain the exact format.

## Testing

`test_delivery_header.py` validates:
- Every delivery produced for 100 consecutive step completions matches the regex.
- State emoji/label mapping is exhaustive (one test per `status` value).
- Rate-limited variant includes correctly formatted `HH:MM`.
- Next-step preview truncation at 80 chars with ellipsis.
- Agent-embedded-header stripping (defensive path).
