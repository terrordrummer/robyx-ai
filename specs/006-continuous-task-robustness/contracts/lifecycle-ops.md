# Contract — Continuous-Task Lifecycle Operations

Defines the four distinct lifecycle operations with unambiguous pre/postconditions, error messages, and journal events.

## State diagram

```
              create                      complete
                │                             ▲
                ▼                             │
            created ──┬──► running ──────────┘
                      │      │
                      │      ▼
                      │  awaiting_input ──reply──► running
                      │      │                       │
                      │      ▼                       ▼
                      │  stopped ◄───stop───────── running
                      │      │                       │
                      │      ▼                       ▼
                      │   resume                 rate_limited ──recovery──► running
                      │      │                       │
                      │      ▼                       ▼
                      └──► running ◄──────────── rate_limit_recovered
                                 │
                                 ▼  (3×orphan backoff)
                              error
                      (any state)
                            │
                            ▼ delete
                        deleted
                      (tombstone, archived_at set)
```

## Operation contracts

### `stop_task(name)` → `[STOP_TASK name="X"]` or `/stop X`

| Aspect | Behaviour |
|---|---|
| **Preconditions** | Task exists; `status ∉ {completed, deleted}` |
| **Effects** | `status = "stopped"`; remove any running subprocess (graceful SIGTERM + drain within `drain_timeout_seconds`); unpin awaiting message if present; update topic title suffix to `⏹`; append `stopped` journal event |
| **Postconditions** | `is_resumable = True`; dedicated topic preserved; queue entry canceled |
| **Idempotency** | Second stop on an already-stopped task: return user-visible "task 'X' is already stopped" and append `stopped` event with outcome=`noop` |
| **Name** | Reserved (not freed) |
| **Errors** | `not_found` (task doesn't exist); `terminal_state` (task is completed or deleted) |

### `resume_task(name)` → `[RESUME_TASK name="X"]` or `/resume X`

| Aspect | Behaviour |
|---|---|
| **Preconditions** | Task exists; `status ∈ {stopped, awaiting_input}` |
| **Effects** | `status = "running"`; clear `awaiting_*` fields on transition from awaiting_input; unpin pinned message; update topic title suffix to `▶`; next scheduler cycle dispatches |
| **Postconditions** | `is_resumable = False` (until next awaiting or stop) |
| **Errors** | `not_found` — user-visible message: `"Task 'X' does not exist. Use [CONTINUOUS name=\"X\" ...] to recreate it, or [GET_EVENTS task=\"X\"] to inspect its archived history."` |
| **Errors** | `invalid_state` — e.g. trying to resume a running task: `"Task 'X' is currently <status>; resume only applies to stopped or awaiting_input tasks."` |

### `complete_task(name)` → `[COMPLETE_TASK name="X"]` or `/complete X`

| Aspect | Behaviour |
|---|---|
| **Preconditions** | Task exists; `status ∉ {completed, deleted}` |
| **Effects** | `status = "completed"`; terminate running subprocess gracefully; update topic title suffix to `✅`; post a "completed" delivery message to the dedicated topic with a final summary if the current step produced output; append `completed` journal event |
| **Postconditions** | `is_terminal = True`; dedicated topic preserved; not resumable |
| **Name** | Reserved (not freed) |
| **Errors** | `not_found`; `terminal_state` |

### `delete_task(name)` → `[DELETE_TASK name="X"]` or `/delete X`

| Aspect | Behaviour |
|---|---|
| **Preconditions** | Task exists in any state |
| **Effects** | In order: (1) terminate running subprocess (bounded by `drain_timeout_seconds`, same as workspace-close drain); (2) post a final "deleted — archiving topic" delivery message to the dedicated topic; (3) call `archive_topic(dedicated_thread_id, display_name)` — renames to `[Archived] <X>` and closes to new messages; (4) remove `agents/<X>.md` agent definition; (5) cancel any queue entries; (6) set `status="deleted"`, `archived_at=<now>`; (7) remove from `AgentManager` registry; (8) append `deleted` event followed by `archived` event to journal |
| **Postconditions** | Name is **immediately free** for a new task; historical topic remains readable on the platform |
| **Idempotency** | Second delete on an already-deleted task: return "task 'X' is already deleted/archived" |
| **Errors** | `not_found` (task never existed — distinguished from "already deleted") |

### `create_continuous(name, program, drain_timeout?)` → `[CONTINUOUS name="X" objective="..." drain_timeout="..."?]`

| Aspect | Behaviour |
|---|---|
| **Preconditions** | `name` passes `_sanitize_task_name`; name not currently reserved by an existing (non-deleted) task; parent workspace thread exists |
| **Effects** | Create git branch; write agent instructions; create dedicated topic `[Continuous] <display_name>`; write state.json (including `drain_timeout_seconds` if provided, else default 3600); append `created` event |
| **Postconditions** | Task listed in `/status`; dedicated topic present with `· ▶` suffix initialised |
| **Errors** | `name_taken` — user-visible message: `"A task named 'X' is already registered in state <current_status>. To reuse this name, issue [DELETE_TASK name=\"X\"] first (archives the topic and frees the name), or choose a different name."` |

## Error-message quality bar

Every lifecycle error surfaced to the user MUST include:
1. The operation attempted
2. The affected task name (quoted)
3. The current actual state (for disambiguation)
4. The concrete next step the user can take

Example golden messages (acceptance-test material):

- `name_taken`: `"Cannot create continuous task 'zeus-research': a task with that name is already registered (state: stopped). To reuse the name, run [DELETE_TASK name=\"zeus-research\"] first (archives the topic and frees the name), or choose a different name."`
- `resume not_found`: `"Cannot resume 'zeus-rd-172': no task with that name exists. If it was previously deleted, its history is available in the archived topic [Archived] zeus-rd-172 and via [GET_EVENTS task=\"zeus-rd-172\"]. To recreate it, use [CONTINUOUS name=\"zeus-rd-172\" objective=\"…\"]."`
- `invalid_state` on resume of a running task: `"Cannot resume 'zeus-research': task is currently running. Resume only applies to stopped or awaiting_input tasks."`

## Journal event contracts

Every lifecycle op MUST append exactly one event at the moment of transition (not at call time). Fields:

| Op | event_type | outcome | payload |
|---|---|---|---|
| stop | `stopped` | `ok` or `noop` | `{"prev_status": "running"}` |
| resume | `resumed` | `from_stopped` or `from_awaiting_input` | `{"prev_status": "..."}` |
| complete | `completed` | `ok` | `{"total_steps": N}` |
| delete | `deleted` | `ok` | `{"archived_thread_id": N}` |
| delete | `archived` | `ok` | `{"new_title": "[Archived] X"}` |
| create | `created` | `ok` | `{"dedicated_thread_id": N, "drain_timeout_seconds": N}` |

## Chat-first surface (Principle II)

All operations reachable from chat via:
- Agent-emitted macros (`[STOP_TASK …]`, etc.)
- User-facing slash commands (`/stop X`, `/resume X`, `/complete X`, `/delete X`)
- Primary-agent interactive lifecycle prompts (existing `lifecycle_macros.py` dispatch — extended to include `/complete` and the new `delete` semantics)
