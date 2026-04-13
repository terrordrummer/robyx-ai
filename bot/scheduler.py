"""Robyx — Scheduler Agent.

Runs every N minutes (default: 10). Reads tasks.md, checks which tasks are due,
verifies lock files, spawns sub-agents as independent AI CLI processes.
Also handles one-shot scheduled tasks (reminders, deadlines).
"""

import asyncio
import inspect
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ai_backend import AIBackend
from config import DATA_DIR, LOG_FILE, TASKS_FILE
from memory import build_memory_context
from model_preferences import resolve_model_preference
from scheduled_delivery import start_task_delivery_watch
from task_runtime import (
    resolve_agent_file_path,
    resolve_task_runtime_context,
    validate_task_name,
)

log = logging.getLogger("robyx.scheduler")

# Frequency → minimum seconds between runs
FREQUENCY_SECONDS = {
    "hourly": 3600,
    "every-6h": 21600,
    "daily": 86400,
    "every-30m": 1800,
    "every-15m": 900,
    "every-10m": 600,
}


def parse_tasks() -> list[dict]:
    """Parse tasks.md into a list of task dicts."""
    if not TASKS_FILE.exists():
        return []

    text = TASKS_FILE.read_text()
    tasks = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| Task") or line.startswith("|--"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 8:
            continue
        tasks.append({
            "name": cols[0],
            "agent_file": cols[1],
            "type": cols[2],
            "frequency": cols[3],
            "enabled": cols[4].lower() == "yes",
            "model": cols[5],
            "thread_id": cols[6],
            "description": cols[7] if len(cols) > 7 else cols[0],
        })
    return tasks


def get_last_run(task_name: str) -> float | None:
    """Get timestamp of last OK or DISPATCHED run from log.txt."""
    if not LOG_FILE.exists():
        return None

    last_time = None
    pattern = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] %s — (OK|DISPATCHED)' % re.escape(task_name))

    for line in LOG_FILE.read_text().splitlines():
        match = pattern.search(line)
        if match:
            try:
                dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
                last_time = dt.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
    return last_time


def is_task_due(task: dict) -> bool:
    """Check if a task should run based on its type and frequency."""
    if not task["enabled"]:
        return False

    # One-shot tasks are handled by the timed scheduler (timed_scheduler.py).
    # The periodic scheduler never runs them.
    if task["type"] in ("one-shot", "interactive"):
        return False

    # Scheduled tasks: check frequency
    freq = task["frequency"]
    if freq == "-":
        return False

    min_interval = FREQUENCY_SECONDS.get(freq)
    if not min_interval:
        log.warning("Unknown frequency '%s' for task '%s'", freq, task["name"])
        return False

    last_run = get_last_run(task["name"])
    if last_run is None:
        return True  # Never run before

    elapsed = time.time() - last_run
    return elapsed >= min_interval


def check_lock(task_name: str) -> tuple[bool, int | None]:
    """Check if a task has an active lock.

    Returns (is_locked, pid).
    Cleans stale locks automatically.
    """
    try:
        safe_name = validate_task_name(task_name)
    except ValueError as exc:
        log.error("Invalid task name for lock check %r: %s", task_name, exc)
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

    # Check if PID is alive
    from process import is_pid_alive, is_ai_process, get_process_name

    if is_pid_alive(pid):
        if is_ai_process(pid):
            return True, pid
        proc_name = get_process_name(pid)
        log.info("Stale lock for '%s': PID %d is now '%s'. Removing.", task_name, pid, proc_name)
        lock_file.unlink(missing_ok=True)
        return False, None
    else:
        log.info("Stale lock for '%s': PID %d is dead. Removing.", task_name, pid)
        lock_file.unlink(missing_ok=True)
        return False, None


async def spawn_task(task: dict, backend: AIBackend, platform=None) -> int | None:
    """Spawn a task as an independent AI CLI process. Returns PID or None."""
    try:
        task_name = validate_task_name(task["name"])
        normalized_agent_file, _agent_type, agent_file = resolve_agent_file_path(
            DATA_DIR, task["agent_file"],
        )
    except ValueError as exc:
        log.error("Invalid scheduled task config for %r: %s", task.get("name"), exc)
        return None

    task_for_runtime = dict(task)
    task_for_runtime["name"] = task_name
    task_for_runtime["agent_file"] = normalized_agent_file

    runtime = resolve_task_runtime_context(task_for_runtime)
    lock_file = DATA_DIR / task_name / "lock"
    output_log = DATA_DIR / task_name / "output.log"
    # Tasks may store either a backend-specific model id or a semantic
    # alias (fast/balanced/powerful). Resolve to a concrete id for the
    # active backend.
    model = resolve_model_preference(
        task.get("model"), backend, role=task.get("type"),
    )

    # Ensure data dir exists
    (DATA_DIR / task_name).mkdir(parents=True, exist_ok=True)

    if not agent_file.exists():
        log.error("Agent file not found: %s", agent_file)
        return None

    # Build the prompt
    agent_instructions = agent_file.read_text()
    memory_ctx = build_memory_context(
        runtime.agent_name,
        runtime.agent_type,
        runtime.work_dir,
    )
    prompt = (
        "You are a scheduled sub-agent for task '%s'.\n\n"
        "Execute the following instructions completely and autonomously:\n\n"
        "---\n%s\n---\n\n"
        "%s\n\n"
        "WHEN DONE (success or failure):\n"
        "1. Append to %s:\n"
        "   [current date and time] %s — OK — <brief summary>\n"
        "   (use ERROR instead of OK if you failed)\n"
        "2. Delete your lock file: `import pathlib; pathlib.Path('%s').unlink(missing_ok=True)`\n"
        "   Or on Unix: rm -f %s\n"
        "3. Always delete the lock file, even on error."
    ) % (task_name, agent_instructions, memory_ctx, LOG_FILE, task_name, lock_file, lock_file)

    cmd = backend.build_spawn_command(
        prompt=prompt,
        model=model,
        work_dir=runtime.work_dir,
    )
    stdin_payload = backend.spawn_stdin_payload(prompt)

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

        # Write lock file
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lock_file.write_text("%d %s" % (proc.pid, now))
        start_task_delivery_watch(
            task,
            proc,
            output_log,
            lock_file,
            platform,
            backend,
            log,
        )

        log.info("Spawned '%s' (PID %d, model: %s)", task_name, proc.pid, model)
        return proc.pid

    except Exception as e:
        log.error("Failed to spawn '%s': %s", task_name, e)
        return None


def append_log(entry: str):
    """Append an entry to log.txt."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a") as f:
        f.write("[%s] %s\n" % (now, entry))


async def run_scheduler_cycle(backend: AIBackend, platform=None) -> dict:
    """Run one scheduler cycle. Returns summary of actions taken."""
    tasks = parse_tasks()
    dispatched = []
    skipped = []
    errors = []

    for task in tasks:
        if not task["enabled"]:
            continue

        name = task["name"]
        try:
            log_name = validate_task_name(name)
        except ValueError:
            log_name = "<invalid-task-name>"

        if not is_task_due(task):
            skipped.append((name, "not due"))
            continue

        is_locked, pid = check_lock(name)
        if is_locked:
            skipped.append((name, "running (PID %d)" % pid))
            append_log("%s — SKIPPED — Agent still running (PID %d)" % (log_name, pid))
            continue

        spawned_pid = await spawn_task(task, backend, platform=platform)
        if spawned_pid:
            dispatched.append((name, spawned_pid))
            append_log("%s — DISPATCHED — Spawned as PID %d" % (log_name, spawned_pid))
        else:
            errors.append(name)
            append_log("%s — ERROR — Failed to spawn" % log_name)

    if not dispatched and not errors:
        append_log("SCHEDULER — IDLE — No tasks due this cycle")

    return {"dispatched": dispatched, "skipped": skipped, "errors": errors}
