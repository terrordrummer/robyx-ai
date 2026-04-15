# Scheduler

← [Back to README](../README.md)

Robyx has a **unified scheduler** that runs every 60 seconds (configurable via `SCHEDULER_INTERVAL`). It manages everything that happens automatically — from simple reminders to long-running autonomous tasks. All entries live in a single `data/queue.json` file.

## What the scheduler can do

**Reminders** — plain text delivered at an exact time, no AI involved. Any agent can schedule one with the `[REMIND ...]` pattern. "Remind me Thursday at 9am — dentist appointment" just works. Survives restarts, no LLM invocation needed.

**One-shot tasks** — an agent subprocess that runs once at a specific date/time. Use this when you need an agent to *do work* at a scheduled moment: "Run a security scan tonight at 2am", "Generate the weekly report next Monday at 8am".

**Periodic tasks** — recurring agent invocations on an interval (hourly, daily, etc.). A system monitor that checks server health every 6 hours, a price tracker that runs every 30 minutes — the scheduler keeps firing them until the workspace is closed or paused.

**Continuous tasks (agentic loop)** — autonomous, iterative work that the scheduler keeps alive step-by-step until the objective is reached or the user intervenes. Each continuous task gets:
- A **dedicated workspace topic** (prefixed with 🔄)
- A **git branch** in the target project's repo
- A **state file** tracking progress, completed steps, and the plan for the next step

The scheduler dispatches one step at a time. Each step: executes, commits its changes, updates the state, and plans the next step. The scheduler picks up the next step on the following cycle. This is how you can say "refactor the auth module into smaller files" and walk away — the agent works through it methodically, one step at a time.

### Starting a continuous task

Two ways:

1. **Explicit** — write `/loop` in a conversation with a workspace agent. The agent interprets the context and enters the setup interview (objective, success criteria, constraints, checkpoint policy, first step).
2. **Conversational** — describe work that is inherently iterative (R&D loops, optimization cycles, progressive refinement). The agent recognizes the pattern and suggests the agentic loop approach. You confirm, and setup begins.

In both cases the agent conducts a structured interview before creating the task — it never launches a long-running iterative workload inline.

## Timing precision

The scheduler ticks every `SCHEDULER_INTERVAL` seconds (default 60). That is the only cadence — there is no per-event wakeup. Consequences:

- **Reminders are fired with up to one tick of delay.** A reminder set for `12:00:00` on a 60-second scheduler actually fires between `12:00:00` and `12:01:00`, whenever the next tick lands. This is fine for human-scale reminders (appointments, deadlines) but not for sub-minute precision. Reduce `SCHEDULER_INTERVAL` if you need tighter timing — at the cost of 60× more disk reads of `data/queue.json`.

- **There is no jitter or drift between bot restarts.** Every tick dispatches every entry whose `fire_at` or `scheduled_at` / `next_run` is already in the past. A reminder whose firing window passed while the bot was down fires on the next tick after restart (late by the outage + up to one tick). Offline recovery is deterministic: **no event is lost**, everything lands as soon as the scheduler wakes up again.

- **Periodic tasks re-arm from the real clock, not from the previous run.** If a daily task was scheduled for `09:00` and fired 20 minutes late at `09:20` (bot was busy or offline), the next run is still set for `next_day 09:00`, not `next_day 09:20`. `_next_run_after()` advances `run_at` by full intervals until it is strictly in the future, so drift does not accumulate.

- **Continuous tasks are not claim-based.** They re-check their state file every tick and spawn the next step whenever `is_ready_for_next_step(state)` is true. Rate-limited tasks retry on the following tick; `awaiting-input` and `paused` states are skipped silently until the user changes them.

## Agent interruption

Any user message to a busy agent **interrupts the running subprocess immediately** (SIGTERM → 5s grace → SIGKILL). Your message is processed right away instead of queuing behind the current task. This works for all agent types — interactive, scheduled, or continuous. You can always stop, redirect, or interact with an agent mid-task.

## Runtime contract

- Each task spawns an independent AI CLI process.
- PID lock files under `data/<task>/lock` prevent duplicate runs and are cleaned both lazily (by `check_lock` during polling) and proactively on the first scheduler cycle of each boot, so locks on workspaces that have no queue entry never accumulate.
- Tasks execute in the target agent's stored `work_dir`.
- Output is logged per-task and relayed back into the target topic/channel.
- An atomic claim system prevents double-dispatch on concurrent access within one process, and a POSIX `fcntl.LOCK_EX` advisory lock on `data/queue.json.lock` prevents two bot processes (e.g. during a rolling restart) from double-claiming the same entry. On non-POSIX systems the file-level lock is a no-op; single-instance deployments remain fully protected by the in-process lock.
- One-shot tasks are marked `dispatched` after firing; closing a workspace cancels all its pending queue entries.
- Reminders that keep failing for longer than `REMINDER_MAX_AGE_SECONDS` (default 24 h) past their `fire_at` are marked `failed` with `failure_reason="expired"` so a persistent delivery failure does not bloat the queue indefinitely.
- The bot also maintains `data/active-pids.json`, a registry of subprocesses it spawned. On startup any survivor that is still alive **and** looks like one of our process names (`claude`, `codex`, `opencode`, `python`, `node`) is force-killed, so a crash during `agent.interrupt()` no longer leaks an unmonitored AI process.

---

← [Back to README](../README.md)
