# Scheduler

← [Back to README](../README.md)

Robyx has a **unified scheduler** that runs every 60 seconds (configurable via `SCHEDULER_INTERVAL`). It manages everything that happens automatically — from simple reminders to long-running autonomous tasks. All entries live in a single `data/queue.json` file.

Queue entries created through Robyx's chat/runtime boundaries persist an
immutable `workspace_scope` containing the platform, canonical chat id, and
parent thread/channel id. Lifecycle reads and mutations compare all three
components; a continuous task's mutable delivery topic is never its ownership
boundary. A legacy record is migrated only when its non-null parent and
configured host prove one unambiguous owner. External code should use the
validated chat macros rather than calling `scheduler.add_task` without scope.

## What the scheduler can do

**Reminders** — plain text delivered at an exact time, no AI involved. Any agent can schedule one with the `[REMIND ...]` pattern. "Remind me Thursday at 9am — dentist appointment" just works. Survives restarts, no LLM invocation needed.

**One-shot tasks** — an agent subprocess that runs once at a specific date/time. Use this when you need an agent to *do work* at a scheduled moment: "Run a security scan tonight at 2am", "Generate the weekly report next Monday at 8am".

**Periodic tasks** — recurring agent invocations on an interval (hourly, daily, etc.). A system monitor that checks server health every 6 hours, a price tracker that runs every 30 minutes — the scheduler keeps firing them until the workspace is closed or paused.

**Continuous tasks (agentic loop)** — autonomous, iterative work that the scheduler keeps alive step-by-step until the objective is reached or the user intervenes. Each continuous task gets:
- A **git branch** in the target project's repo
- A **state file** tracking progress, completed steps, and the plan for the next step
- An authoritative, revisioned **program record** (`data/continuous/<name>/program.json`) plus a repaired-on-load human-readable `plan.md`
- A **dedicated task topic/channel** for step reports, state markers, pinned questions, incidents, and final notices; ownership still comes from the immutable parent `workspace_scope`

The scheduler dispatches one step at a time. Each step: executes, commits its changes, updates the state, and plans the next step. The scheduler picks up the next step on the following cycle. This is how you can say "refactor the auth module into smaller files" and walk away — the agent works through it methodically, one step at a time.

### Starting a continuous task

Two ways:

1. **Explicit** — write `/loop` in a conversation with a workspace agent. The agent interprets the context and enters the setup interview (objective, success criteria, constraints, checkpoint policy, first step).
2. **Conversational** — describe work that is inherently iterative (R&D loops, optimization cycles, progressive refinement). The agent recognizes the pattern and suggests the agentic loop approach. You confirm, and setup begins.

In both cases the agent conducts a structured interview before creating the task — it never launches a long-running iterative workload inline.

### Checkpoint policies

Each continuous task is configured at creation with a `checkpoint_policy` that governs **when the step agent is allowed to stop and hand control back to the user** (via `status: "awaiting-input"`). The policy is binding — the agent does not substitute its own judgement for it.

| Policy | Behaviour |
|--------|-----------|
| `on-demand` (default) | Never stops on the agent's own initiative. The user interrupts from chat when they want to pause or redirect. A genuinely impossible step is marked `status: error`, not `awaiting-input`. |
| `on-uncertainty` | Stops only for **genuinely blocking ambiguity** — "no sensible person could choose without a human decision." Cosmetic doubts or "A or B both look fine" do not qualify. |
| `on-milestone` | Stops only at milestones declared in the plan's `## Milestones` section. If the plan does not declare milestones, behaves like `on-demand`. |
| `every-N-steps` | Stops when the current step number is a multiple of N, where N is read from a "Checkpoint every N steps" line in the plan. If N is not declared, behaves like `on-demand`. |

The scheduler substitutes the active policy into the step-agent prompt on every dispatch (`templates/CONTINUOUS_STEP.md`). You can change the policy of a **running** task in place — see *Controlling tasks from the workspace chat* below.

### Controlling tasks from the workspace chat

You never need a dedicated control panel: everything happens by talking to the **primary workspace agent** in the workspace chat. The agent recognises natural-language lifecycle intents ("ferma daily-report", "pause the research loop", "mostrami il piano di zeus-research") and emits workspace-scoped lifecycle macros that the bot resolves against authoritative state (`data/queue.json` + `data/continuous/*/state.json`) before the response reaches the user.

| Macro | Scope | Effect |
|-------|-------|--------|
| `[LIST_TASKS]` | Any active task in this workspace | Renders a grouped summary of continuous / periodic / one-shot / reminders. |
| `[TASK_STATUS name="…"]` | Any active task | Detailed status — objective, steps completed, constraints, last step (for continuous); next run / fire-at (for scheduled). |
| `[STOP_TASK name="…"]` | Any active task | For a continuous task, cancels future dispatch first, terminates the live step (SIGTERM, at most 5 s grace, then SIGKILL), and records resumable `stopped` state only after the process exits. |
| `[PAUSE_TASK name="…"]` | Continuous only | User-facing alias of stop: the live step is terminated and the task remains resumable. |
| `[RESUME_TASK name="…"]` | Continuous only | Reactivates the canceled queue generation and resumes a `stopped` / `rate-limited` / `awaiting-input` / recoverable `error` task; the next tick dispatches the planned step. |
| `[COMPLETE_TASK name="…"]` | Continuous only | Terminates the live step and commits terminal `completed` state. History and the name remain reserved. |
| `[DELETE_TASK name="…"]` | Continuous only | Terminates the live step, archives the topic when supported, writes a `deleted` tombstone, removes the agent registration, and frees the name for a new generation. |
| `[GET_PLAN name="…"]` | Continuous only | Streams `data/continuous/<name>/plan.md` inline (truncated at ~2000 chars). |
| `[UPDATE_PLAN name="…"]` + `[CONTINUOUS_PROGRAM]{…}[/CONTINUOUS_PROGRAM]` | Continuous only | Revisioned partial update of `objective`, `success_criteria`, `constraints`, `checkpoint_policy`, `context`, or `plan_text`. The accepted program and exact plan body live in `program.json`; stale step snapshots cannot roll them back and `plan.md` is repaired after an interrupted commit. |

All macros are **workspace-scoped**: a task owned by another workspace is reported as `not found` rather than touched, so one workspace can never silently mutate another's state. Ambiguous name queries (substring match) surface a disambiguation prompt instead of acting.

The primary workspace agent also receives an *Active continuous tasks in this workspace* block at the top of its prompt on every turn, listing the tasks it owns with their objective, checkpoint policy, and pending question if any — so when you reply to an `awaiting-input` task, the agent treats your reply as an answer to that task (optionally emitting `[UPDATE_PLAN]` + `[RESUME_TASK]`) rather than spawning a duplicate task with overlapping scope.

## Timing precision

The scheduler ticks every `SCHEDULER_INTERVAL` seconds (default 60). That is the only cadence — there is no per-event wakeup. Consequences:

- **Reminders are fired with up to one tick of delay.** A reminder set for `12:00:00` on a 60-second scheduler actually fires between `12:00:00` and `12:01:00`, whenever the next tick lands. This is fine for human-scale reminders (appointments, deadlines) but not for sub-minute precision. Reduce `SCHEDULER_INTERVAL` if you need tighter timing — at the cost of 60× more disk reads of `data/queue.json`.

- **There is no jitter or drift between bot restarts.** Every tick dispatches every entry whose `fire_at` or `scheduled_at` / `next_run` is already in the past. A reminder whose firing window passed while the bot was down fires on the next tick after restart (late by the outage + up to one tick). Offline recovery is deterministic: **no event is lost**, everything lands as soon as the scheduler wakes up again.

- **Periodic tasks re-arm from the real clock, not from the previous run.** If a daily task was scheduled for `09:00` and fired 20 minutes late at `09:20` (bot was busy or offline), the next run is still set for `next_day 09:00`, not `next_day 09:20`. `_next_run_after()` advances `run_at` by full intervals until it is strictly in the future, so drift does not accumulate.

- **Continuous tasks are not claim-based.** They re-check their state file every tick and spawn the next step whenever `is_ready_for_next_step(state)` is true. Rate-limited tasks retry on the following tick; `awaiting_input` and `stopped` states (plus their legacy aliases) are skipped silently until the user changes them.

## Agent interruption

Any user message to a busy agent **interrupts the running subprocess immediately** (SIGTERM → 5s grace → SIGKILL). Your message is processed right away instead of queuing behind the current task. This works for all agent types — interactive, scheduled, or continuous. You can always stop, redirect, or interact with an agent mid-task.

## Runtime contract

- Each task spawns an independent AI CLI process.
- Lifecycle mutations for the same continuous-task name are serialized. State is
  reloaded after subprocess termination, and a post-spawn generation check reaps
  a child if stop/delete won while it was being created. A deleted name can be
  recreated without leaving an ambiguous canceled queue entry.
- PID lock files under `data/<task>/lock` prevent duplicate runs and are cleaned both lazily (by `check_lock` during polling) and proactively on the first scheduler cycle of each boot, so locks on workspaces that have no queue entry never accumulate.
- Tasks execute in the target agent's stored `work_dir`.
- Output is logged per-task and relayed back into the target topic/channel.
- Long-lived scheduler/update loops and every scheduled-delivery watcher are
  retained by the runtime supervisor. Watchers still wait/reap and remove the
  lock when no messaging platform is attached; uncaught task exceptions are
  logged instead of disappearing into a detached `asyncio.Task`.
- An atomic claim system prevents double-dispatch on concurrent access within one process, and a POSIX `fcntl.LOCK_EX` advisory lock on `data/queue.json.lock` prevents two bot processes (e.g. during a rolling restart) from double-claiming the same entry. On non-POSIX systems the file-level lock is a no-op; single-instance deployments remain fully protected by the in-process lock.
- One-shot tasks are marked `dispatched` after firing; closing a workspace cancels all its pending queue entries.
- Reminders that keep failing for longer than `REMINDER_MAX_AGE_SECONDS` (default 7 d, raised from 24 h in v0.20.28) past their `fire_at` are marked `failed` with `failure_reason="expired"` so a persistent delivery failure does not bloat the queue indefinitely.
- A task stuck in `dispatching` longer than `CLAIM_TIMEOUT_SECONDS` (default 600 s, raised from 300 s in v0.20.28) has its claim reset so the next cycle can re-dispatch. Results arriving with a stale claim token are logged loudly rather than silently applied.
- The bot also maintains `data/active-pids.json`, a registry of subprocesses it spawned. On startup any survivor that is still alive **and** looks like one of our process names (`claude`, `codex`, `opencode`, `python`, `node`) is force-killed, so a crash during `agent.interrupt()` no longer leaks an unmonitored AI process. Since v0.20.28 the bot spawns every CLI with `start_new_session=True` and signals the whole process group during interrupt, so grandchildren (a `node` worker spawned by a CLI, etc.) are reaped with their parent instead of surviving as re-parented orphans.
- On normal adapter shutdown, the supervisor first refuses new child work,
  sends SIGTERM to every isolated AI process group, escalates to SIGKILL after
  a bounded grace period, reaps the immediate children, then cancels and
  drains its background tasks. SIGTERM/atexit also has a bounded synchronous
  fallback if the event loop is already unavailable.
- Periodic recovery: a periodic task that has missed N intervals while the bot was offline fires **once** on resume (the currently-due instance), and its `next_run` is advanced past `now`. The intermediate missed instances are intentionally skipped to avoid a thundering-herd on startup.

## Corrupt-state recovery

`queue.json` is never silently replaced with an empty list after a decode or
shape error. Robyx quarantines the original bytes, searches updater snapshots
from newest to oldest for a verified copy, and installs that one file atomically.
The same recovery path protects every continuous task's `state.json`.

If no valid snapshot exists, the scheduler reports a degraded
`queue_unavailable` cycle and all queue mutations fail closed. A durable
`.recovery-pending` marker lets the next boot resume recovery if the process
stops between quarantine and install. Recovery can roll state back to the
snapshot time, so the CRITICAL log and event require operator reconciliation.

---

← [Back to README](../README.md)
