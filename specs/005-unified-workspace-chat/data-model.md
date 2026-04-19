# Phase 1 Data Model — Unified Workspace Chat

## Entity summary

| Entity | Source file | Change scope |
|--------|-------------|--------------|
| Workspace | `data/workspaces.json` (existing) | No change |
| ContinuousTask state | `data/continuous/<name>/state.json` | Field repoint + new `migrated_v0_23_0` marker + new `plan_path` |
| ContinuousTask plan | `data/continuous/<name>/plan.md` | NEW artifact |
| Scheduler Queue entry | `data/queue.json` (existing) | No schema change; consumers read `type` to drive marker |
| Migration state | `data/migrations/v0_23_0.done` + per-state `migrated_v0_23_0` | NEW, additive |

## ContinuousTask state.json

### Current shape (from `bot/continuous.py::create_continuous_task`, pre-0.23.0)

> **Note**: the authoritative field names come from the running codebase. The
> delivery target is `workspace_thread_id` (NOT `thread_id`); the program
> body is a nested `program` dict; step state lives in `next_step` /
> `current_step` / `history`.

```jsonc
{
  "id": "uuid…",
  "name": "daily-report",
  "status": "running",                  // pending | running | paused | awaiting-input | completed | error | rate-limited
  "parent_workspace": "ops",
  "workspace_thread_id": 18,            // TODAY: legacy sub-topic thread id (🔄 <name>)
  "branch": "continuous/daily-report",
  "work_dir": "/abs/path",
  "created_at": "2026-04-13T09:00:00Z",
  "updated_at": "2026-04-13T09:00:00Z",
  "program": {
    "objective": "...",
    "success_criteria": ["..."],
    "constraints": ["..."],
    "checkpoint_policy": "on-demand",
    "context": "..."
  },
  "current_step": null,
  "next_step": { "number": 1, "description": "..." },
  "history": [ /* step objects */ ],
  "total_steps_completed": 0,
  "rate_limited_until": null,
  "versioning": "git-branch"
}
```

### After migration v0_23_0

```jsonc
{
  // all existing fields preserved, plus:
  "workspace_thread_id": 2,              // CHANGED: now the parent workspace thread
  "legacy_workspace_thread_id": 18,      // NEW: preserves old sub-topic id for audit
  "plan_path": "data/continuous/daily-report/plan.md",   // NEW: per-task plan artifact
  "migrated_v0_23_0": "2026-04-19T14:22:10Z"              // NEW: idempotency marker
}
```

### Validation rules

- `workspace_thread_id` MUST reference an open channel/thread on the target platform. If unresolvable at dispatch time, the scheduler logs an error and SKIPS the step (does NOT delete state).
- `legacy_workspace_thread_id` is read-only after migration.
- `plan_path` MUST be a relative path (anchored at repo root) to ensure portability across machines.
- `migrated_v0_23_0` is an ISO-8601 UTC timestamp; its presence implies all previous fields are in the post-0.23.0 shape.
- `status ∈ {pending, running, paused, awaiting-input, completed, error, rate-limited}`. `stop` command transitions to `completed` (explicit user stop); `pause` transitions to `paused`; `resume` uses the existing `resume_task()` helper which clears rate-limit state and transitions back to `pending`.

### State transitions

```text
                  ┌──────── stop ────────────────┐
                  │                              ▼
  pending ──► running ──► running ──► …    completed
    ▲            │                              ▲
    │            └── pause ──► paused ──┐       │
    │                                   │       │
    └───────── resume ◄──────────────────┘       │
                                                 │
                 error ◄── step exit != 0        │
                 │                               │
                 └────────── resume / stop ──────┘
```

## ContinuousTask plan.md (NEW)

Markdown document persisted at `data/continuous/<name>/plan.md` at task creation.

**Structure (authored by the primary agent at creation time)**:

```markdown
# Plan: <task-name>

## Objective
<agreed objective>

## Success criteria
- …

## Constraints
- …

## Stop conditions
- …

## Step structure
<free-form description of how iterations are structured>

## Notes
<anything else captured during the clarification exchange>
```

**Readers**:
- Primary agent (on `[GET_PLAN name=…]` lifecycle macro or equivalent natural-language request) — summarizes into chat.
- Secondary agent (scheduler-spawned) — loaded verbatim into its prompt alongside `agents/<name>.md` and `state.json`.

## Queue entry (unchanged schema, new consumer logic)

Entries in `data/queue.json` carry `"type": "continuous" | "periodic" | "one-shot" | "reminder"`. The delivery layer reads this to pick the icon marker. No new fields required.

## Migration state artifacts

### `data/migrations/v0_23_0.done`

Single empty file written by `bot/migrations/v0_23_0.py` after all tasks in the input directory have been processed (success OR recorded error). Presence of this file causes the migration runner to short-circuit on subsequent launches.

### Per-state `migrated_v0_23_0`

Per-task idempotency marker (see shape above). Re-running the migration re-reads each state file and SKIPS any whose `migrated_v0_23_0` field is already set.

## Workspace registry (unchanged)

`data/workspaces.json` continues to store `{chat_id, thread_id, name, …}` per workspace. The migration resolves the parent workspace via the continuous task's existing `workspace_name` (or `parent_workspace_name`, if present) field; if the workspace is not found, the task is skipped and the error is recorded in the migration log.

## Invariants preserved across this change

- Atomic write-then-rename for every state file (via `bot/continuous.py` existing helper).
- No partial-write window: plan.md is written with the same atomic pattern.
- Scheduler queue semantics (claim tokens, late-fire on restart, single-instance lock) unchanged.
- Workspace registry schema unchanged.
