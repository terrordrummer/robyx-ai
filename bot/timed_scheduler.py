"""Robyx — Timed Task Scheduler.

High-frequency scheduler (default: 60 s). Reads data/timed_queue.json,
dispatches tasks whose scheduled_at / next_run <= now, and marks them so
they are never run twice.

Types
-----
one-shot  : run once at scheduled_at, then status -> "dispatched".
periodic  : run at next_run, then next_run advances by interval_seconds.

Race-condition safety
---------------------
All queue mutations use atomic write-then-rename (os.replace), which is
guaranteed atomic on POSIX.  The file is never left in a half-written
state even if the process is killed mid-write.

Jitter / offline recovery
--------------------------
Tasks whose scheduled_at / next_run is in the past are treated as due
immediately on startup.  They are dispatched in the first cycle so no
event is ever permanently missed due to a system restart or brief outage.
"""

import asyncio
import inspect
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from ai_backend import AIBackend
from config import CLAIM_TIMEOUT_SECONDS, DATA_DIR, LOG_FILE, TASKS_FILE, TIMED_QUEUE_FILE
from memory import build_memory_context
from model_preferences import resolve_model_preference
from scheduled_delivery import start_task_delivery_watch
from task_runtime import (
    resolve_agent_file_path,
    resolve_task_runtime_context,
    validate_agent_file_ref,
    validate_task_name,
)

log = logging.getLogger("robyx.timed_scheduler")
_queue_lock = threading.Lock()


# -- Queue I/O ----------------------------------------------------------------


def _load_queue_unlocked() -> list[dict]:
    """Load the timed task queue. Returns [] if the file is missing or corrupt."""
    if not TIMED_QUEUE_FILE.exists():
        return []
    try:
        return json.loads(TIMED_QUEUE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load timed queue: %s", exc)
        return []


def load_queue() -> list[dict]:
    with _queue_lock:
        return _load_queue_unlocked()


def _save_queue_unlocked(tasks: list[dict]) -> None:
    """Atomically persist the timed task queue (write-then-rename)."""
    TIMED_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TIMED_QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2))
    os.replace(tmp, TIMED_QUEUE_FILE)


def save_queue(tasks: list[dict]) -> None:
    with _queue_lock:
        _save_queue_unlocked(tasks)


def validate_one_shot_scheduled_at(
    scheduled_at: str | None, *, label: str = "one-shot task"
) -> str:
    """Return a normalized ISO datetime for a one-shot task schedule.

    Missing placeholders like ``none`` / ``-`` are rejected so callers do
    not produce dead queue entries. Naive datetimes are interpreted as UTC
    to stay compatible with the scheduler's existing read path.
    """
    if scheduled_at is None:
        raise ValueError("scheduled_at is required for %s" % label)

    value = str(scheduled_at).strip()
    if not value or value == "-" or value.lower() == "none":
        raise ValueError("scheduled_at is required for %s" % label)

    try:
        scheduled_dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "scheduled_at for %s must be a valid ISO datetime" % label
        ) from exc

    if scheduled_dt.tzinfo is None:
        scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)

    return scheduled_dt.isoformat()


def add_task(task: dict) -> None:
    """Append a single task to the queue atomically.

    Required fields
    ---------------
    name         : str  -- unique slug (used for lock file, log entries)
    agent_file   : str  -- semantic brief ref: "agents/foo.md" or "specialists/foo.md"
    type         : str  -- "one-shot" | "periodic"
    scheduled_at : str  -- ISO-8601 UTC datetime (for one-shot and first periodic run)
    model        : str  -- semantic alias or backend-specific model id

    Optional fields
    ---------------
    prompt           : str  -- override prompt; if empty the agent file is used verbatim
    thread_id        : str  -- messaging thread/channel ID for context
    interval_seconds : int  -- required when type == "periodic"
    description      : str  -- human-readable label
    """
    queued_task = dict(task)
    queued_task["name"] = validate_task_name(queued_task.get("name", ""))
    queued_task["agent_file"], _agent_type = validate_agent_file_ref(
        queued_task.get("agent_file", ""),
    )
    if queued_task.get("type", "one-shot") == "one-shot":
        queued_task["scheduled_at"] = validate_one_shot_scheduled_at(
            queued_task.get("scheduled_at"),
            label="one-shot tasks",
        )

    queued_task.setdefault("id", str(uuid.uuid4()))
    queued_task.setdefault("status", "pending")
    queued_task.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    with _queue_lock:
        tasks = _load_queue_unlocked()
        tasks.append(queued_task)
        _save_queue_unlocked(tasks)


def cancel_tasks_for_agent_file(
    agent_file: str, *, reason: str = "workspace closed"
) -> int:
    """Mark pending timed-queue entries targeting ``agent_file`` as canceled."""
    if not agent_file:
        return 0

    with _queue_lock:
        tasks = _load_queue_unlocked()
        canceled = 0
        canceled_at = datetime.now(timezone.utc).isoformat()

        for task in tasks:
            if task.get("status") != "pending":
                continue
            if task.get("agent_file") != agent_file:
                continue
            task["status"] = "canceled"
            task["canceled_at"] = canceled_at
            task["canceled_reason"] = reason
            canceled += 1

        if canceled:
            _save_queue_unlocked(tasks)
            log.info(
                "Canceled %d pending timed task(s) for %s (%s)",
                canceled,
                agent_file,
                reason,
            )

    return canceled


# -- Scheduling logic ---------------------------------------------------------


def find_due(tasks: list[dict]) -> list[dict]:
    """Return tasks that are due: (scheduled_at or next_run) <= now and status == pending."""
    now = datetime.now(timezone.utc)
    due = []
    for task in tasks:
        if task.get("status") != "pending":
            continue
        run_at_str = task.get("scheduled_at") or task.get("next_run")
        if not run_at_str:
            continue
        try:
            run_at = datetime.fromisoformat(run_at_str)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            if now >= run_at:
                due.append(task)
        except ValueError:
            log.warning("Invalid date for task '%s': %s", task.get("name"), run_at_str)
    return due


def _next_run_after(run_at: datetime, interval_seconds: int) -> datetime:
    """Advance run_at by full intervals until it is strictly in the future."""
    now = datetime.now(timezone.utc)
    delta = timedelta(seconds=interval_seconds)
    while run_at <= now:
        run_at += delta
    return run_at


def _check_lock(task_name: str) -> tuple[bool, int | None]:
    """Check whether a timed task already has a live worker process."""
    try:
        safe_name = validate_task_name(task_name)
    except ValueError as exc:
        log.error("Invalid timed task name for lock check %r: %s", task_name, exc)
        return False, None

    lock_file = DATA_DIR / safe_name / "lock"
    if not lock_file.exists():
        return False, None

    try:
        content = lock_file.read_text().strip()
        parts = content.split()
        pid = int(parts[0])
    except (ValueError, IndexError):
        lock_file.unlink(missing_ok=True)
        return False, None

    from process import get_process_name, is_ai_process, is_pid_alive

    if is_pid_alive(pid):
        if is_ai_process(pid):
            return True, pid
        proc_name = get_process_name(pid)
        log.info(
            "Stale timed-task lock for '%s': PID %d is now '%s'. Removing.",
            task_name, pid, proc_name,
        )
        lock_file.unlink(missing_ok=True)
        return False, None

    log.info("Stale timed-task lock for '%s': PID %d is dead. Removing.", task_name, pid)
    lock_file.unlink(missing_ok=True)
    return False, None


def _clear_claim(task: dict) -> None:
    task.pop("claim_token", None)
    task.pop("claimed_at", None)


def _reset_stale_claims(tasks: list[dict], now: datetime) -> bool:
    changed = False
    for task in tasks:
        if task.get("status") != "dispatching":
            continue

        claimed_at_raw = task.get("claimed_at")
        try:
            claimed_at = datetime.fromisoformat(claimed_at_raw) if claimed_at_raw else None
            if claimed_at and claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        except ValueError:
            claimed_at = None

        if claimed_at is None or (now - claimed_at).total_seconds() > CLAIM_TIMEOUT_SECONDS:
            log.warning(
                "Resetting stale timed-task claim %s back to pending",
                task.get("id"),
            )
            task["status"] = "pending"
            _clear_claim(task)
            changed = True

    return changed


def _claim_due_tasks() -> list[dict]:
    """Reserve due tasks under the file lock so they cannot dispatch twice."""
    with _queue_lock:
        tasks = _load_queue_unlocked()
        if not tasks:
            return []

        now = datetime.now(timezone.utc)
        changed = _reset_stale_claims(tasks, now)
        due: list[dict] = []

        for task in tasks:
            if task.get("status") != "pending":
                continue

            run_at_str = task.get("scheduled_at") or task.get("next_run")
            if not run_at_str:
                continue

            try:
                run_at = datetime.fromisoformat(run_at_str)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning("Invalid date for task '%s': %s", task.get("name"), run_at_str)
                task["status"] = "invalid"
                changed = True
                continue

            if now < run_at:
                continue

            claim_token = uuid.uuid4().hex
            task["status"] = "dispatching"
            task["claim_token"] = claim_token
            task["claimed_at"] = now.isoformat()
            changed = True
            due.append(dict(task))

        if changed:
            _save_queue_unlocked(tasks)

        return due


def _reconcile_dispatch_results(results: list[dict]) -> None:
    """Merge dispatch outcomes back into the latest queue state."""
    if not results:
        return

    with _queue_lock:
        tasks = _load_queue_unlocked()
        if not tasks:
            return

        changed = False
        for result in results:
            task = next(
                (
                    item for item in tasks
                    if item.get("id") == result["id"]
                    and item.get("claim_token") == result["claim_token"]
                ),
                None,
            )
            if task is None:
                log.warning(
                    "Timed task %s changed before reconciliation; skipping stale claim",
                    result["id"],
                )
                continue

            if result["status"] == "dispatched":
                if result["task_type"] == "periodic":
                    interval = task.get("interval_seconds", 86400)
                    run_at_str = task.get("scheduled_at") or task.get("next_run", "")
                    try:
                        run_at = datetime.fromisoformat(run_at_str)
                        if run_at.tzinfo is None:
                            run_at = run_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        run_at = datetime.now(timezone.utc)
                    task["next_run"] = _next_run_after(run_at, interval).isoformat()
                    task.pop("scheduled_at", None)
                    task["status"] = "pending"
                else:
                    task["status"] = "dispatched"
            elif result["status"] == "pending":
                task["status"] = "pending"
            else:
                task["status"] = "error"

            _clear_claim(task)
            changed = True

        if changed:
            _save_queue_unlocked(tasks)


# -- Dispatch -----------------------------------------------------------------


async def dispatch_task(task: dict, backend: AIBackend, platform=None) -> int | None:
    """Spawn a timed task as an independent AI CLI process. Returns PID or None."""
    try:
        task_name = validate_task_name(task["name"])
        normalized_agent_file, _agent_type, agent_file_path = resolve_agent_file_path(
            DATA_DIR, task["agent_file"],
        )
    except ValueError as exc:
        log.error("Invalid timed task config for %r: %s", task.get("name"), exc)
        return None

    task_for_runtime = dict(task)
    task_for_runtime["name"] = task_name
    task_for_runtime["agent_file"] = normalized_agent_file

    runtime = resolve_task_runtime_context(task_for_runtime)
    lock_file = DATA_DIR / task_name / "lock"
    output_log = DATA_DIR / task_name / "output.log"
    # Resolve semantic aliases (fast/balanced/powerful) into a concrete
    # model id for the active backend, falling back to the queued value or
    # the role default if neither is set.
    model = resolve_model_preference(
        task.get("model"), backend, role=task.get("type", "one-shot"),
    )
    prompt_override = task.get("prompt", "")

    (DATA_DIR / task_name).mkdir(parents=True, exist_ok=True)

    if not agent_file_path.exists():
        log.error("Agent file not found: %s", agent_file_path)
        return None

    agent_instructions = agent_file_path.read_text()
    memory_ctx = build_memory_context(
        runtime.agent_name,
        runtime.agent_type,
        runtime.work_dir,
    )

    if prompt_override:
        full_prompt = (
            "You are a scheduled sub-agent for task '%s'.\n\n"
            "Your specific task for this run:\n%s\n\n"
            "Context from agent instructions:\n---\n%s\n---\n\n"
            "%s\n\n"
            "WHEN DONE (success or failure):\n"
            "1. Append to %s:\n"
            "   [current date and time] %s -- OK -- <brief summary>\n"
            "   (use ERROR instead of OK if you failed)\n"
            "2. Delete your lock file: rm -f %s\n"
            "3. Always delete the lock file, even on error."
        ) % (task_name, prompt_override, agent_instructions, memory_ctx,
             LOG_FILE, task_name, lock_file)
    else:
        full_prompt = (
            "You are a scheduled sub-agent for task '%s'.\n\n"
            "Execute the following instructions completely and autonomously:\n\n"
            "---\n%s\n---\n\n"
            "%s\n\n"
            "WHEN DONE (success or failure):\n"
            "1. Append to %s:\n"
            "   [current date and time] %s -- OK -- <brief summary>\n"
            "   (use ERROR instead of OK if you failed)\n"
            "2. Delete your lock file: rm -f %s\n"
            "3. Always delete the lock file, even on error."
        ) % (task_name, agent_instructions, memory_ctx,
             LOG_FILE, task_name, lock_file)

    cmd = backend.build_spawn_command(
        prompt=full_prompt,
        model=model,
        work_dir=runtime.work_dir,
    )
    stdin_payload = backend.spawn_stdin_payload(full_prompt)

    try:
        with open(output_log, "w") as out_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else asyncio.subprocess.DEVNULL,
                stdout=out_f,
                stderr=asyncio.subprocess.STDOUT,
                cwd=runtime.work_dir,
            )
            if stdin_payload is not None and proc.stdin is not None:
                write_result = proc.stdin.write(stdin_payload)
                if inspect.isawaitable(write_result):
                    await write_result
                await proc.stdin.drain()
                close_result = proc.stdin.close()
                if inspect.isawaitable(close_result):
                    await close_result

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lock_file.write_text("%d %s" % (proc.pid, now_str))
        start_task_delivery_watch(
            task,
            proc,
            output_log,
            lock_file,
            platform,
            backend,
            log,
        )

        log.info("Timed scheduler: spawned '%s' (PID %d, model: %s)",
                 task_name, proc.pid, model)
        return proc.pid

    except (OSError, ValueError) as exc:
        log.error("Timed scheduler: failed to spawn '%s': %s", task_name, exc, exc_info=True)
        return None


# -- Log helper ---------------------------------------------------------------


def _append_log(entry: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a") as fh:
        fh.write("[%s] %s\n" % (now, entry))


# -- Main cycle ---------------------------------------------------------------


async def run_timed_cycle(backend: AIBackend, platform=None) -> dict:
    """Run one timed-scheduler cycle.

    Loads the queue, finds due tasks, dispatches them, updates their status
    atomically, and returns a summary dict.
    """
    due = _claim_due_tasks()

    dispatched: list[tuple[str, int]] = []
    errors: list[str] = []
    results: list[dict] = []

    for task in due:
        task_name = task["name"]
        task_type = task.get("type", "one-shot")

        try:
            safe_task_name = validate_task_name(task_name)
        except ValueError as exc:
            errors.append(str(task_name))
            _append_log("TIMED <invalid-task-name> -- ERROR -- %s" % exc)
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "error",
                "task_type": task_type,
            })
            continue

        is_locked, existing_pid = _check_lock(safe_task_name)
        if is_locked:
            _append_log(
                "TIMED %s -- SKIPPED -- Agent still running (PID %d)"
                % (safe_task_name, existing_pid)
            )
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "pending",
                "task_type": task_type,
            })
            continue

        pid = await dispatch_task(task, backend, platform=platform)
        if pid:
            dispatched.append((task_name, pid))
            _append_log("TIMED %s -- DISPATCHED -- PID %d" % (task_name, pid))
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "dispatched",
                "task_type": task_type,
            })
        else:
            errors.append(task_name)
            _append_log("TIMED %s -- ERROR -- Failed to spawn" % task_name)
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "error",
                "task_type": task_type,
            })

    _reconcile_dispatch_results(results)

    return {"dispatched": dispatched, "errors": errors}


# -- Startup migration --------------------------------------------------------


def migrate_oneshot_from_tasks_md() -> int:
    """Migrate one-shot rows from tasks.md into the timed queue.

    Each migrated row is disabled in tasks.md so the periodic scheduler
    never sees it again.  Returns the number of tasks migrated.
    """
    if not TASKS_FILE.exists():
        return 0

    migrated = 0
    lines = TASKS_FILE.read_text().splitlines()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Skip headers, separators, and non-data lines
        if (
            not stripped.startswith("|")
            or stripped.startswith("| Task")
            or stripped.startswith("|--")
        ):
            new_lines.append(line)
            continue

        cols = [c.strip() for c in stripped.split("|")[1:-1]]
        if len(cols) < 8:
            new_lines.append(line)
            continue

        task_type = cols[2]
        enabled = cols[4].lower() == "yes"

        if task_type == "one-shot" and enabled:
            sched_str = cols[3]  # frequency column holds scheduled_at for one-shots
            if sched_str and sched_str != "-":
                try:
                    scheduled_dt = datetime.fromisoformat(sched_str)
                    if scheduled_dt.tzinfo is None:
                        scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)

                    add_task({
                        "id": str(uuid.uuid4()),
                        "name": cols[0],
                        "agent_file": cols[1],
                        "prompt": "",
                        "type": "one-shot",
                        "scheduled_at": scheduled_dt.isoformat(),
                        "status": "pending",
                        "model": cols[5],
                        "thread_id": cols[6],
                        "description": cols[7] if len(cols) > 7 else cols[0],
                        "migrated_from_tasks_md": True,
                    })
                    migrated += 1
                    log.info("Migrated one-shot task '%s' to timed queue", cols[0])
                    line = line.replace("| yes |", "| no |", 1)
                except ValueError as exc:
                    log.warning(
                        "Could not migrate one-shot '%s' to timed queue: %s",
                        cols[0], exc,
                    )

        new_lines.append(line)

    if migrated:
        TASKS_FILE.write_text("\n".join(new_lines) + "\n")
        log.info("Migration complete: %d one-shot task(s) moved to timed queue", migrated)

    return migrated
