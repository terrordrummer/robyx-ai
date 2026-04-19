# Contract — Lifecycle Macros

## Purpose

The primary workspace agent recognizes natural-language requests about task lifecycle and emits one of five server-processed macros. A server-side handler resolves the macro against authoritative state (`queue.json` + `data/continuous/*/state.json`), applies the action, and substitutes the macro with a rendered text response. The rendered response is then post-processed by the existing user-facing chokepoint (stripping, platform formatting, sending) like any other primary-agent reply.

This contract defines macro grammar, resolution rules, and response shape.

## Grammar

Macros appear inline in the primary agent's response text and use the same bracketed form as `CREATE_CONTINUOUS`:

```text
[LIST_TASKS]
[TASK_STATUS name="<task-name>"]
[STOP_TASK name="<task-name>"]
[PAUSE_TASK name="<task-name>"]
[RESUME_TASK name="<task-name>"]
[GET_PLAN name="<task-name>"]                # optional P3 helper for FR-013
```

- Names MAY use single or smart quotes (`"…"`, `'…'`, `“…”`). The parser normalizes.
- Case-insensitive on the macro keyword. `name=` attribute is case-sensitive on the value.
- A macro MUST be the ONLY significant content in the agent's response — the response before macro substitution may contain adjacent prose, but after substitution the rendered result replaces the whole block including the macro invocation.
- Malformed macros (missing `name`, unknown keyword) are stripped from the user-visible output and logged at WARN. The agent MAY be re-prompted on the next turn.

## Scope

All macros are **workspace-scoped**: the handler reads the current `chat_id` + `thread_id` from the handler context and filters candidate tasks by matching `(chat_id, thread_id)` against each task's stored workspace reference. Tasks in other workspaces are invisible to these macros.

## Semantics

### `[LIST_TASKS]`

Returns a grouped summary of all tasks in the current workspace whose status is NOT in `{completed}`. Groups in order: continuous, periodic, one-shot, reminder. Within each group, sort by `next_run` ascending (continuous tasks by last-step timestamp descending).

**Rendered format**:

```markdown
**Task attivi nel workspace** (<count>)

🔄 *Continuous*
- `daily-report` — running · last step: 14:05 · obj: "…"
- `doc-hunt` — paused

⏰ *Periodic*
- `check-metrics` — next run: 15:00 (every 1h)

📌 *One-shot*
- `deploy-staging` — scheduled 2026-04-20 09:00

🔔 *Reminder*
- `standup prep` — fires 2026-04-20 08:55
```

Empty groups are omitted. If no tasks exist at all: `"Nessun task attivo nel workspace."`

### `[TASK_STATUS name="…"]`

Returns detailed status for exactly one task.

- If 0 matches → `"Nessun task attivo chiamato `<name>` nel workspace."`
- If 1 match → detailed status (objective, current status, last step summary or next run, history length, active constraints). For non-continuous tasks, only the relevant subset.
- If ≥2 matches → render the disambiguation prompt (see §Disambiguation).

### `[STOP_TASK name="…"]`

Transitions the target's status to `completed`, persists, and ceases future dispatches.

- 0 matches → "Nessun task …"
- 1 match → `"Task `<name>` fermato."` confirmation; state persisted atomically.
- ≥2 matches → disambiguation.

### `[PAUSE_TASK name="…"]`

Transitions the target's status to `paused`.

- Same match rules as `STOP_TASK`.
- Confirmation: `"Task `<name>` in pausa. Riprendi con: ripristina `<name>`."`

### `[RESUME_TASK name="…"]`

Transitions the target's status from `paused` back to `pending` (continuous) or to active schedule (periodic/one-shot that were paused).

- 0 matches among paused tasks → suggest currently-active tasks.
- 1 match → `"Task `<name>` ripreso."`
- ≥2 matches → disambiguation.

### `[GET_PLAN name="…"]` (P3, optional)

Reads `data/continuous/<name>/plan.md` for the resolved task and returns it verbatim (summarized if > 2000 chars). For non-continuous task types, returns: `"Il piano dettagliato è disponibile solo per task continuativi."`

## Disambiguation (§Disambiguation)

When a command matches ≥2 tasks (case-insensitive substring match on `name`), the handler renders a numbered list and ends with a clarifying question:

```markdown
Ho trovato più task che corrispondono a "<query>":

1. 🔄 `daily-report` (continuous, running)
2. ⏰ `weekly-report` (periodic, next run: 09:00 lunedì)

Quale intendi?
```

The primary agent treats the user's next message as the disambiguation answer. On the next turn it re-emits the same macro with the resolved exact name. No special "continuation" protocol is required — the primary's existing context is enough.

**Matching rules for disambiguation input**:
- User replies with number (`"1"`) → resolved to the corresponding entry.
- User replies with partial name → re-run match; if still ambiguous, disambiguate again.
- User replies "annulla" / "cancel" / "lascia stare" → no action, confirmation "Ok, non faccio nulla."

## Resolution against authoritative state

Handler MUST:
1. Read `data/queue.json` once (under lock) and filter `entries` by `chat_id` + `thread_id` matching the current workspace.
2. For continuous entries, load the referenced `data/continuous/<name>/state.json` for detailed fields.
3. Case-insensitive substring match on `name` against the candidate set.
4. Apply the mutation atomically (queue write or state write) via the existing helpers in `bot/scheduler.py` / `bot/continuous.py`.
5. Log an INFO record per action: `{ts, workspace, macro, name, resolved_to, outcome}`.

## Error handling

- Missing state file for a queue entry → log ERROR, render `"Stato interno incoerente per `<name>`. Ho registrato l'errore."`, do NOT mutate.
- Atomic write failure → render `"Errore nell'aggiornamento dello stato di `<name>`. Nessuna modifica applicata."`, log ERROR with exception info, return.

## Non-goals

- No authorization checks — any user in the workspace chat may invoke lifecycle macros (per spec, out of scope).
- No cross-workspace operations — deliberately rejected by workspace scoping.
- No history/audit beyond existing log records — not in this feature's scope.
