# Scheduler

← [Back to README](../README.md)

Robyx has a **unified scheduler** that runs every 60 seconds (configurable via `SCHEDULER_INTERVAL`). It manages everything that happens automatically — from simple reminders to long-running autonomous tasks. All entries live in a single `data/queue.json` file.

## What the scheduler can do

**Reminders** — plain text delivered at an exact time, no AI involved. Any agent can schedule one with the `[REMIND ...]` pattern. "Remind me Thursday at 9am — dentist appointment" just works. Survives restarts, no LLM invocation needed.

**One-shot tasks** — an agent subprocess that runs once at a specific date/time. Use this when you need an agent to *do work* at a scheduled moment: "Run a security scan tonight at 2am", "Generate the weekly report next Monday at 8am".

**Periodic tasks** — recurring agent invocations on an interval (hourly, daily, etc.). A system monitor that checks server health every 6 hours, a price tracker that runs every 30 minutes — the scheduler keeps firing them until the workspace is closed or paused.

**Continuous tasks** — autonomous, iterative work that the scheduler keeps alive step-by-step until the objective is reached or the user intervenes. Each continuous task gets:
- A **dedicated workspace topic** (prefixed with 🔄)
- A **git branch** in the target project's repo
- A **state file** tracking progress, completed steps, and the plan for the next step

The scheduler dispatches one step at a time. Each step: executes, commits its changes, updates the state, and plans the next step. The scheduler picks up the next step on the following cycle. This is how you can say "refactor the auth module into smaller files" and walk away — the agent works through it methodically, one step at a time.

## Agent interruption

Any user message to a busy agent **interrupts the running subprocess immediately** (SIGTERM → 5s grace → SIGKILL). Your message is processed right away instead of queuing behind the current task. This works for all agent types — interactive, scheduled, or continuous. You can always stop, redirect, or interact with an agent mid-task.

## Runtime contract

- Each task spawns an independent AI CLI process.
- PID lock files under `data/<task>/lock` prevent duplicate runs and clean stale locks.
- Tasks execute in the target agent's stored `work_dir`.
- Output is logged per-task and relayed back into the target topic/channel.
- An atomic claim system prevents double-dispatch on concurrent access.
- One-shot tasks are marked `dispatched` after firing; closing a workspace cancels all its pending queue entries.

---

← [Back to README](../README.md)
