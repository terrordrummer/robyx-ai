"""Robyx — Unified Scheduler.

Single scheduler that runs every 60 seconds. Reads ``data/queue.json`` and
dispatches all task types: reminders, one-shot tasks, periodic tasks, and
continuous tasks.

Queue entry types
-----------------
reminder   : plain text delivery (no LLM), fires at ``fire_at``
one-shot   : agent subprocess at ``scheduled_at``, then status -> "dispatched"
periodic   : agent subprocess at ``next_run``, then next_run advances
continuous : iterative autonomous work (dispatched via bot.continuous)

Race-condition safety
---------------------
All queue mutations use atomic write-then-rename (``os.replace``).
The claim system prevents double-dispatch even on concurrent access.

Offline recovery
----------------
Tasks with due times in the past are dispatched on the next cycle.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # POSIX only; on Windows we fall back to thread-lock-only.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from ai_backend import AIBackend
from config import (
    CLAIM_TIMEOUT_SECONDS,
    DATA_DIR,
    LOG_FILE,
    MAX_REMINDER_ATTEMPTS,
    QUEUE_FILE,
    REMINDER_MAX_AGE_SECONDS,
    TASKS_FILE,
    TIMED_QUEUE_FILE,
)
from memory import build_memory_context
from model_preferences import resolve_model_preference
from scheduled_delivery import start_task_delivery_watch
from task_runtime import (
    resolve_agent_file_path,
    resolve_task_runtime_context,
    validate_agent_file_ref,
    validate_task_name,
)

log = logging.getLogger("robyx.scheduler")

SEND_TIMEOUT_SECONDS = 30

# Frequency label -> seconds (used only during migration from tasks.md)
FREQUENCY_SECONDS = {
    "hourly": 3600,
    "every-6h": 21600,
    "daily": 86400,
    "every-30m": 1800,
    "every-15m": 900,
    "every-10m": 600,
}

_queue_lock = threading.Lock()


@contextlib.contextmanager
def _queue_mutex():
    """Acquire intra-process + inter-process exclusive access to the queue.

    Holds ``_queue_lock`` (threads in this process) **and** a POSIX
    ``fcntl.LOCK_EX`` on a sidecar lockfile (other bot processes). On
    non-POSIX systems the file-level lock is a no-op; the thread lock
    alone still protects single-instance deployments.
    """
    with _queue_lock:
        if fcntl is None:
            yield
            return
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = QUEUE_FILE.with_name(QUEUE_FILE.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# ── Queue I/O ────────────────────────────────────────────────────────────────


def _load_queue_unlocked() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load queue.json: %s", exc)
        return []


def load_queue() -> list[dict]:
    with _queue_mutex():
        return _load_queue_unlocked()


# Size at which the next queue scan is expensive enough to warrant a
# heads-up. At 500 entries the full-list scan under ``_queue_mutex``
# starts adding perceptible latency to every mutation; an explicit log
# line lets operators archive or prune before the scheduler tick starts
# missing its 60 s budget. Purely observational — no behaviour change.
_QUEUE_SIZE_WARN = 500
_queue_size_warned = False


def _save_queue_unlocked(entries: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    os.replace(tmp, QUEUE_FILE)

    global _queue_size_warned
    if len(entries) >= _QUEUE_SIZE_WARN and not _queue_size_warned:
        _queue_size_warned = True
        log.warning(
            "Queue has %d entries (warn threshold %d). Full-list scans "
            "start dominating scheduler cycle cost beyond this point; "
            "consider pruning dispatched/failed entries or archiving.",
            len(entries), _QUEUE_SIZE_WARN,
        )
    elif len(entries) < _QUEUE_SIZE_WARN // 2:
        # Reset so the next time the queue grows again we warn once more.
        _queue_size_warned = False


def save_queue(entries: list[dict]) -> None:
    with _queue_mutex():
        _save_queue_unlocked(entries)


# ── Validation helpers ───────────────────────────────────────────────────────


def validate_one_shot_scheduled_at(
    scheduled_at: str | None, *, label: str = "one-shot task"
) -> str:
    """Return a normalized ISO datetime. Rejects placeholders like ``none``/``-``."""
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


# ── Public API: add entries ──────────────────────────────────────────────────


def add_task(task: dict) -> None:
    """Append a one-shot, periodic, or continuous task to the queue atomically.

    Required: ``name``, ``agent_file``, ``type``, ``scheduled_at`` (one-shot).
    """
    queued = dict(task)
    queued["name"] = validate_task_name(queued.get("name", ""))

    task_type = queued.get("type", "one-shot")

    # Continuous tasks don't need agent_file validation at queue level
    if task_type != "continuous":
        queued["agent_file"], _agent_type = validate_agent_file_ref(
            queued.get("agent_file", ""),
        )
    if task_type == "one-shot":
        queued["scheduled_at"] = validate_one_shot_scheduled_at(
            queued.get("scheduled_at"),
            label="one-shot tasks",
        )

    queued.setdefault("id", str(_uuid.uuid4()))
    queued.setdefault("status", "pending")
    queued.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    with _queue_mutex():
        entries = _load_queue_unlocked()
        entries.append(queued)
        _save_queue_unlocked(entries)


def add_reminder(entry: dict) -> None:
    """Append a reminder entry to the queue atomically.

    Required: ``fire_at``, ``message``. Optional: ``chat_id``, ``thread_id``.
    """
    queued = dict(entry)
    queued["type"] = "reminder"
    queued.setdefault("id", str(_uuid.uuid4()))
    queued.setdefault("status", "pending")
    queued.setdefault("attempts", 0)
    queued.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    with _queue_mutex():
        entries = _load_queue_unlocked()
        entries.append(queued)
        _save_queue_unlocked(entries)


def cancel_tasks_for_agent_file(
    agent_file: str, *, reason: str = "workspace closed"
) -> int:
    """Mark pending entries targeting ``agent_file`` as canceled."""
    if not agent_file:
        return 0

    with _queue_mutex():
        entries = _load_queue_unlocked()
        canceled = 0
        canceled_at = datetime.now(timezone.utc).isoformat()

        for entry in entries:
            if entry.get("status") != "pending":
                continue
            if entry.get("agent_file") != agent_file:
                continue
            entry["status"] = "canceled"
            entry["canceled_at"] = canceled_at
            entry["canceled_reason"] = reason
            canceled += 1

        if canceled:
            _save_queue_unlocked(entries)
            log.info(
                "Canceled %d pending task(s) for %s (%s)",
                canceled, agent_file, reason,
            )

    return canceled


# Alias for backward compatibility
cancel_tasks_for_agent = cancel_tasks_for_agent_file


# ── Startup cleanup ──────────────────────────────────────────────────────────


async def cleanup_stale_locks_on_startup() -> list[str]:
    """Remove lock files left behind by crashed subprocesses.

    A crash between writing a lock file and the subprocess actually exiting
    leaves a ``data/<task>/lock`` pointing at a dead PID. ``check_lock()``
    already cleans these lazily, but only for tasks that are actively
    checked during the scheduler cycle — a workspace that has no pending
    queue entry never hits check_lock and its stale lock lingers.

    At bot startup we proactively scan every ``data/*/lock`` and remove
    those whose PID is not alive or is not an AI subprocess. Returns the
    list of task names that had their lock cleaned, for logging.
    """
    from process import get_process_name, is_ai_process, is_pid_alive

    if not DATA_DIR.exists():
        return []

    cleaned: list[str] = []
    for lock_file in DATA_DIR.glob("*/lock"):
        task_name = lock_file.parent.name
        try:
            content = lock_file.read_text().strip()
            pid = int(content.split()[0])
        except (OSError, ValueError, IndexError):
            lock_file.unlink(missing_ok=True)
            cleaned.append(task_name)
            continue

        if not is_pid_alive(pid):
            lock_file.unlink(missing_ok=True)
            cleaned.append(task_name)
            log.info("Startup cleanup: removed stale lock for '%s' (PID %d dead)", task_name, pid)
            continue

        if not await is_ai_process(pid):
            proc_name = await get_process_name(pid)
            lock_file.unlink(missing_ok=True)
            cleaned.append(task_name)
            log.info(
                "Startup cleanup: removed stale lock for '%s' (PID %d recycled as '%s')",
                task_name, pid, proc_name,
            )

    if cleaned:
        log.info("Startup cleanup: removed %d stale lock(s): %s", len(cleaned), ", ".join(cleaned))
    return cleaned


# ── Lock files ───────────────────────────────────────────────────────────────


async def check_lock(task_name: str) -> tuple[bool, int | None]:
    """Check if a task has an active lock. Cleans stale locks automatically."""
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

    from process import get_process_name, is_ai_process, is_pid_alive

    if is_pid_alive(pid):
        if await is_ai_process(pid):
            return True, pid
        proc_name = await get_process_name(pid)
        log.info("Stale lock for '%s': PID %d is now '%s'. Removing.", task_name, pid, proc_name)
        lock_file.unlink(missing_ok=True)
        return False, None

    log.info("Stale lock for '%s': PID %d is dead. Removing.", task_name, pid)
    lock_file.unlink(missing_ok=True)
    return False, None


# ── Claim system ─────────────────────────────────────────────────────────────


def _parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _clear_claim(entry: dict) -> None:
    entry.pop("claim_token", None)
    entry.pop("claimed_at", None)


def _reset_stale_claims(entries: list[dict], now: datetime) -> bool:
    """Reset entries stuck in dispatching/sending for longer than CLAIM_TIMEOUT."""
    changed = False
    for entry in entries:
        status = entry.get("status")
        if status not in ("dispatching", "sending"):
            continue

        claimed_at_raw = entry.get("claimed_at")
        try:
            claimed_at = _parse_timestamp(claimed_at_raw) if claimed_at_raw else None
        except ValueError:
            claimed_at = None

        if claimed_at is None or (now - claimed_at).total_seconds() > CLAIM_TIMEOUT_SECONDS:
            log.warning(
                "Resetting stale claim %s (%s) back to pending",
                entry.get("id"), entry.get("type"),
            )
            entry["status"] = "pending"
            _clear_claim(entry)
            changed = True

    return changed


def _claim_due_entries() -> tuple[list[dict], list[dict]]:
    """Reserve due entries under the file lock.

    Returns (due_tasks, due_reminders) — each entry carries a claim_token.
    """
    with _queue_mutex():
        entries = _load_queue_unlocked()
        if not entries:
            return [], []

        now = datetime.now(timezone.utc)
        changed = _reset_stale_claims(entries, now)
        due_tasks: list[dict] = []
        due_reminders: list[dict] = []

        for entry in entries:
            if entry.get("status") != "pending":
                continue

            entry_type = entry.get("type", "one-shot")

            if entry_type == "reminder":
                # Check fire_at
                try:
                    fire_at = _parse_timestamp(entry["fire_at"])
                except (KeyError, ValueError) as e:
                    log.warning("Invalid fire_at for reminder %s: %s", entry.get("id"), e)
                    entry["status"] = "invalid"
                    changed = True
                    continue

                if fire_at > now:
                    continue

                # Reject reminders whose fire_at is too far in the past to
                # still be relevant. Without this guard, a reminder that
                # keeps failing (e.g. transient network error on every
                # attempt) could linger in the queue retrying for days.
                age_seconds = (now - fire_at).total_seconds()
                if age_seconds > REMINDER_MAX_AGE_SECONDS:
                    log.warning(
                        "Reminder %s expired (%.0fs past fire_at, limit %ds), marking failed",
                        entry.get("id"), age_seconds, REMINDER_MAX_AGE_SECONDS,
                    )
                    entry["status"] = "failed"
                    entry["failure_reason"] = "expired"
                    changed = True
                    continue

                attempts = entry.get("attempts", 0)
                if attempts >= MAX_REMINDER_ATTEMPTS:
                    log.warning(
                        "Reminder %s exceeded max attempts (%d), marking failed",
                        entry.get("id"), MAX_REMINDER_ATTEMPTS,
                    )
                    entry["status"] = "failed"
                    entry["failure_reason"] = "max-attempts"
                    changed = True
                    continue

                claim_token = _uuid.uuid4().hex
                entry["status"] = "sending"
                entry["claim_token"] = claim_token
                entry["claimed_at"] = now.isoformat()
                entry["attempts"] = attempts + 1
                changed = True

                due_reminders.append({
                    "id": entry.get("id"),
                    "claim_token": claim_token,
                    "chat_id": entry.get("chat_id"),
                    "thread_id": entry.get("thread_id"),
                    "message": entry.get("message", ""),
                    "late_seconds": (now - fire_at).total_seconds(),
                })

            elif entry_type == "continuous":
                # Continuous tasks are claimed differently — handled in
                # _handle_continuous_entries() after the main claim pass.
                continue

            else:
                # one-shot / periodic — check scheduled_at or next_run
                run_at_str = entry.get("scheduled_at") or entry.get("next_run")
                if not run_at_str:
                    continue

                try:
                    run_at = _parse_timestamp(run_at_str)
                except ValueError:
                    log.warning("Invalid date for task '%s': %s", entry.get("name"), run_at_str)
                    entry["status"] = "invalid"
                    changed = True
                    continue

                if now < run_at:
                    continue

                claim_token = _uuid.uuid4().hex
                entry["status"] = "dispatching"
                entry["claim_token"] = claim_token
                entry["claimed_at"] = now.isoformat()
                changed = True
                due_tasks.append(dict(entry))

        if changed:
            _save_queue_unlocked(entries)

        return due_tasks, due_reminders


def _next_run_after(run_at: datetime, interval_seconds: int) -> datetime:
    """Advance run_at by full intervals until it is strictly in the future."""
    now = datetime.now(timezone.utc)
    delta = timedelta(seconds=interval_seconds)
    while run_at <= now:
        run_at += delta
    return run_at


def _reconcile_task_results(results: list[dict]) -> None:
    """Merge dispatch outcomes back into the queue."""
    if not results:
        return

    with _queue_mutex():
        entries = _load_queue_unlocked()
        if not entries:
            return

        changed = False
        for result in results:
            entry = next(
                (
                    item for item in entries
                    if item.get("id") == result["id"]
                    and item.get("claim_token") == result["claim_token"]
                ),
                None,
            )
            if entry is None:
                # Claim did not reconcile. Two scenarios:
                # 1. Entry fully removed (user cancelled) — acceptable.
                # 2. Claim token mismatch (stale-claim reset or concurrent
                #    mutation from another bot instance) — serious: if the
                #    result status was "dispatched", the task actually ran
                #    but we cannot record it, so the next cycle may run it
                #    again. Log at ERROR so it is visible.
                current = next(
                    (item for item in entries if item.get("id") == result["id"]),
                    None,
                )
                if current is None:
                    log.info(
                        "Task %s reconciliation skipped: entry removed",
                        result["id"],
                    )
                elif result.get("status") == "dispatched":
                    log.error(
                        "Task %s dispatched but claim token stale "
                        "(current status=%s). Possible duplicate dispatch "
                        "on next cycle. Check for concurrent bot instances.",
                        result["id"], current.get("status"),
                    )
                else:
                    log.warning(
                        "Task %s reconciliation skipped: claim token stale "
                        "(current status=%s, result status=%s)",
                        result["id"], current.get("status"), result.get("status"),
                    )
                continue

            if result["status"] == "dispatched":
                # A task is recurring iff it carries an ``interval_seconds``;
                # the legacy ``type="periodic"`` string used to drive this
                # branch is now purely informational. Tasks without an
                # interval are one-shot and terminate in ``dispatched``.
                interval = entry.get("interval_seconds")
                if interval:
                    run_at_str = entry.get("scheduled_at") or entry.get("next_run", "")
                    try:
                        run_at = _parse_timestamp(run_at_str)
                    except ValueError:
                        run_at = datetime.now(timezone.utc)
                    entry["next_run"] = _next_run_after(run_at, interval).isoformat()
                    entry.pop("scheduled_at", None)
                    entry["status"] = "pending"
                else:
                    entry["status"] = "dispatched"
            elif result["status"] == "pending":
                entry["status"] = "pending"
            else:
                entry["status"] = "error"

            _clear_claim(entry)
            changed = True

        if changed:
            _save_queue_unlocked(entries)


def _reconcile_reminder_results(results: list[dict]) -> None:
    """Merge reminder send outcomes back into the queue."""
    if not results:
        return

    with _queue_mutex():
        entries = _load_queue_unlocked()
        if not entries:
            return

        changed = False
        for result in results:
            entry = next(
                (
                    item for item in entries
                    if item.get("id") == result["id"]
                    and item.get("claim_token") == result["claim_token"]
                ),
                None,
            )
            if entry is None:
                log.warning(
                    "Reminder %s changed before reconciliation; skipping",
                    result["id"],
                )
                continue

            if result["status"] == "sent":
                entry["status"] = "sent"
                entry["sent_at"] = result["sent_at"]
                late_by = result.get("late_by_seconds")
                if late_by is not None:
                    entry["late_by_seconds"] = late_by
                else:
                    entry.pop("late_by_seconds", None)
            else:
                entry["status"] = "pending"
                entry.pop("sent_at", None)
                entry.pop("late_by_seconds", None)

            _clear_claim(entry)
            changed = True

        if changed:
            _save_queue_unlocked(entries)


# ── Dispatch: reminders ──────────────────────────────────────────────────────


async def _dispatch_reminders(
    due_reminders: list[dict],
    platform: Any,
    default_chat_id: Any = None,
) -> None:
    """Send due reminders and reconcile results."""
    results = []
    for reminder in due_reminders:
        chat_id = reminder["chat_id"] or default_chat_id
        if chat_id is None:
            log.warning("Reminder %s has no destination", reminder["id"])
            results.append({
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "pending",
            })
            continue

        try:
            sent = await asyncio.wait_for(
                platform.send_message(
                    chat_id=chat_id,
                    text=reminder["message"],
                    thread_id=reminder["thread_id"],
                    parse_mode="markdown",
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
            if sent is None:
                log.error("Failed to send reminder %s: no message ref", reminder["id"])
                results.append({
                    "id": reminder["id"],
                    "claim_token": reminder["claim_token"],
                    "status": "pending",
                })
                continue

            sent_at = datetime.now(timezone.utc)
            result: dict[str, Any] = {
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "sent",
                "sent_at": sent_at.isoformat(),
            }
            if reminder["late_seconds"] > 120:
                result["late_by_seconds"] = int(reminder["late_seconds"])
                log.info(
                    "Reminder %s fired (%.0fs late): %s",
                    reminder["id"], reminder["late_seconds"],
                    reminder["message"][:60],
                )
            else:
                log.info(
                    "Reminder %s fired on time: %s",
                    reminder["id"], reminder["message"][:60],
                )
            results.append(result)

        except asyncio.TimeoutError:
            log.error("Reminder %s send timed out after %ds", reminder["id"], SEND_TIMEOUT_SECONDS)
            results.append({
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "pending",
            })
        except (OSError, RuntimeError) as e:
            log.error("Failed to send reminder %s: %s", reminder["id"], e)
            results.append({
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "pending",
            })

    _reconcile_reminder_results(results)


# ── Shared spawn helpers ────────────────────────────────────────────────────


async def _spawn_ai_subprocess(
    *,
    cmd: list[str],
    stdin_payload: bytes | str | None,
    output_log: Path,
    work_dir: str,
) -> asyncio.subprocess.Process:
    """Run an AI CLI subprocess, redirecting stdout+stderr to ``output_log``.

    Centralises the boilerplate shared by the one-shot/periodic path and
    the continuous-task path: open the log, launch the CLI, pipe the
    prompt payload into stdin (tolerating both sync and async
    ``write`` / ``close`` variants that asyncio StreamWriter exposes).
    """
    with open(output_log, "w") as out_f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=(
                asyncio.subprocess.PIPE if stdin_payload is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=out_f,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
        )
        if stdin_payload is not None and proc.stdin is not None:
            write_result = proc.stdin.write(stdin_payload)
            if inspect.isawaitable(write_result):
                await write_result
            await proc.stdin.drain()
            close_result = proc.stdin.close()
            if inspect.isawaitable(close_result):
                await close_result
    return proc


def _write_lock_file(lock_file: Path, pid: int) -> None:
    """Write ``<pid> <ISO-utc>`` to the task lock file."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lock_file.write_text("%d %s" % (pid, now_str))


# ── Dispatch: agent tasks (one-shot / periodic) ─────────────────────────────


async def _spawn_agent_task(task: dict, backend: AIBackend, platform=None) -> int | None:
    """Spawn a one-shot or periodic task as an independent AI CLI process."""
    try:
        task_name = validate_task_name(task["name"])
        normalized_agent_file, _agent_type, agent_file_path = resolve_agent_file_path(
            DATA_DIR, task["agent_file"],
        )
    except ValueError as exc:
        log.error("Invalid task config for %r: %s", task.get("name"), exc)
        return None

    task_for_runtime = dict(task)
    task_for_runtime["name"] = task_name
    task_for_runtime["agent_file"] = normalized_agent_file

    runtime = resolve_task_runtime_context(task_for_runtime)
    lock_file = DATA_DIR / task_name / "lock"
    output_log = DATA_DIR / task_name / "output.log"
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
        proc = await _spawn_ai_subprocess(
            cmd=cmd,
            stdin_payload=stdin_payload,
            output_log=output_log,
            work_dir=runtime.work_dir,
        )
        _write_lock_file(lock_file, proc.pid)
        start_task_delivery_watch(task, proc, output_log, lock_file, platform, backend, log)

        log.info("Spawned '%s' (PID %d, model: %s)", task_name, proc.pid, model)
        return proc.pid

    except (OSError, ValueError) as exc:
        log.error("Failed to spawn '%s': %s", task_name, exc, exc_info=True)
        return None


async def _dispatch_agent_tasks(
    due_tasks: list[dict],
    backend: AIBackend,
    platform=None,
) -> tuple[list[tuple[str, int]], list[str]]:
    """Dispatch one-shot and periodic agent tasks. Returns (dispatched, errors)."""
    dispatched: list[tuple[str, int]] = []
    errors: list[str] = []
    results: list[dict] = []

    for task in due_tasks:
        task_name = task["name"]
        task_type = task.get("type", "one-shot")

        try:
            safe_name = validate_task_name(task_name)
        except ValueError as exc:
            errors.append(str(task_name))
            append_log("<invalid-task-name> -- ERROR -- %s" % exc)
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "error",
                "task_type": task_type,
            })
            continue

        is_locked, existing_pid = await check_lock(safe_name)
        if is_locked:
            append_log("%s -- SKIPPED -- Agent still running (PID %d)" % (safe_name, existing_pid))
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "pending",
                "task_type": task_type,
            })
            continue

        pid = await _spawn_agent_task(task, backend, platform=platform)
        if pid:
            dispatched.append((task_name, pid))
            append_log("%s -- DISPATCHED -- PID %d" % (task_name, pid))
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "dispatched",
                "task_type": task_type,
            })
        else:
            errors.append(task_name)
            append_log("%s -- ERROR -- Failed to spawn" % task_name)
            results.append({
                "id": task["id"],
                "claim_token": task.get("claim_token"),
                "status": "error",
                "task_type": task_type,
            })

    _reconcile_task_results(results)
    return dispatched, errors


# ── Dispatch: continuous tasks ────────────────────────────────────────────────


async def _handle_continuous_entries(backend: AIBackend, platform=None) -> tuple[list[tuple[str, int]], list[str]]:
    """Check continuous entries in the queue and dispatch next steps if ready.

    Continuous tasks are NOT claimed via the normal claim system because
    they stay in ``pending`` status perpetually (the scheduler re-dispatches
    them every cycle). Instead we check each entry's state file directly.
    """
    from continuous import (
        build_step_context,
        check_rate_limit_recovery,
        is_ready_for_next_step,
        load_state,
        mark_step_failed,
        mark_step_started,
        resume_task,
        save_state,
        state_file_path,
    )

    entries = load_queue()
    dispatched: list[tuple[str, int]] = []
    errors: list[str] = []

    for entry in entries:
        if entry.get("type") != "continuous":
            continue
        if entry.get("status") != "pending":
            continue

        name = entry.get("name", "")
        sf = entry.get("state_file")
        if not sf:
            sf = str(state_file_path(name))

        state = load_state(Path(sf))
        if state is None:
            log.warning("Continuous task '%s': state file missing at %s", name, sf)
            continue

        # Handle rate-limited tasks
        if state["status"] == "rate-limited":
            if check_rate_limit_recovery(state):
                resume_task(state)
                save_state(Path(sf), state)
                log.info("Continuous task '%s': rate limit recovered, resuming", name)
            else:
                continue

        # Skip if not ready
        if state["status"] in ("completed", "paused", "awaiting-input"):
            continue

        # Check if a subprocess is actually running (orphan detection)
        if state["status"] == "running":
            is_locked, pid = await check_lock(name)
            if is_locked:
                continue  # Subprocess still running
            # Subprocess died without updating state
            log.warning("Continuous task '%s': state=running but no lock. Marking step failed.", name)
            mark_step_failed(state, "subprocess exited unexpectedly")
            save_state(Path(sf), state)
            continue

        if not is_ready_for_next_step(state):
            continue

        # Dispatch next step
        next_step = state.get("next_step", {})
        step_number = next_step.get("number", 1)
        step_description = next_step.get("description", "Continue work.")

        # Build prompt from template
        template_path = Path(__file__).parent.parent / "templates" / "CONTINUOUS_STEP.md"
        if template_path.exists():
            template = template_path.read_text()
        else:
            template = "Execute step {{STEP_NUMBER}}: {{STEP_DESCRIPTION}}"

        program = state.get("program", {})
        criteria_text = "\n".join("- %s" % c for c in program.get("success_criteria", []))
        constraints_text = "\n".join("- %s" % c for c in program.get("constraints", []))
        history_text = build_step_context(state)

        lock_file = DATA_DIR / name / "lock"

        # Build versioning instructions based on git availability
        versioning = state.get("versioning", "none")
        branch = state.get("branch", "main")
        if versioning in ("git-branch", "git-init"):
            versioning_instructions = (
                "You are working on branch `%s`. Commit your changes after each step:\n"
                "```\n"
                "git add -A && git commit -m \"continuous(%s): step %d — <brief description>\"\n"
                "```\n"
                "Record the commit hash in the state file's `history[].artifact` field."
                % (branch, name, step_number)
            )
        else:
            versioning_instructions = (
                "Git is not available in this work directory. "
                "Do not attempt git commands. Track progress only via the state file."
            )

        prompt = (
            template
            .replace("{{OBJECTIVE}}", program.get("objective", ""))
            .replace("{{SUCCESS_CRITERIA}}", criteria_text or "(none specified)")
            .replace("{{CONSTRAINTS}}", constraints_text or "(none specified)")
            .replace("{{CONTEXT}}", program.get("context", ""))
            .replace("{{STEP_NUMBER}}", str(step_number))
            .replace("{{STEP_DESCRIPTION}}", step_description)
            .replace("{{STEP_HISTORY}}", history_text)
            .replace("{{VERSIONING_INSTRUCTIONS}}", versioning_instructions)
            .replace("{{STATE_FILE}}", sf)
            .replace("{{TASK_NAME}}", name)
            .replace("{{LOCK_FILE}}", str(lock_file))
            .replace("{{LOG_FILE}}", str(LOG_FILE))
        )

        # Spawn the step agent
        model = resolve_model_preference(
            entry.get("model"), backend, role="continuous",
        )
        work_dir = state.get("work_dir", "")

        cmd = backend.build_spawn_command(
            prompt=prompt,
            model=model,
            work_dir=work_dir,
        )
        stdin_payload = backend.spawn_stdin_payload(prompt)

        (DATA_DIR / name).mkdir(parents=True, exist_ok=True)
        output_log = DATA_DIR / name / "output.log"

        try:
            proc = await _spawn_ai_subprocess(
                cmd=cmd,
                stdin_payload=stdin_payload,
                output_log=output_log,
                work_dir=work_dir,
            )

            # Persist state=running BEFORE writing the lock file. If we
            # crash between the two writes, the next scheduler cycle sees
            # state="running" and triggers the orphan-recovery branch above
            # (check_lock → mark_step_failed). The reverse order would leak
            # a stale lock with a dead PID and leave state in a pre-running
            # status, allowing a silent re-dispatch that overwrites
            # output.log and stomps on the prior attempt.
            mark_step_started(state, step_number, step_description)
            save_state(Path(sf), state)

            _write_lock_file(lock_file, proc.pid)

            # Start delivery watcher for output relay
            start_task_delivery_watch(entry, proc, output_log, lock_file, platform, backend, log)

            dispatched.append((name, proc.pid))
            append_log("%s -- DISPATCHED -- step %d PID %d" % (name, step_number, proc.pid))
            log.info(
                "Continuous '%s': dispatched step %d (PID %d, model: %s)",
                name, step_number, proc.pid, model,
            )

        except (OSError, ValueError) as exc:
            errors.append(name)
            append_log("%s -- ERROR -- step %d failed to spawn: %s" % (name, step_number, exc))
            log.error("Continuous '%s': failed to spawn step %d: %s", name, step_number, exc, exc_info=True)

    return dispatched, errors


# ── Log helper ───────────────────────────────────────────────────────────────


def append_log(entry: str) -> None:
    """Append a timestamped entry to bot.log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a") as f:
        f.write("[%s] %s\n" % (now, entry))


# ── Query helpers ────────────────────────────────────────────────────────────


async def get_running_tasks() -> list[dict]:
    """Return queue entries whose lock file indicates a running subprocess."""
    entries = load_queue()
    running = []
    for entry in entries:
        entry_type = entry.get("type", "one-shot")
        if entry_type == "reminder":
            continue
        name = entry.get("name", "")
        if not name:
            continue
        is_locked, pid = await check_lock(name)
        if is_locked:
            running.append({**entry, "_pid": pid})
    return running


# ── Main cycle ───────────────────────────────────────────────────────────────


_startup_cleanup_done = False


async def run_scheduler_cycle(
    backend: AIBackend,
    platform=None,
    default_chat_id: Any = None,
) -> dict:
    """Run one unified scheduler cycle.

    Returns a summary dict with dispatched tasks, errors, and reminder counts.
    """
    global _startup_cleanup_done
    if not _startup_cleanup_done:
        _startup_cleanup_done = True
        try:
            await cleanup_stale_locks_on_startup()
        except Exception as exc:
            log.error("Startup lock cleanup failed: %s", exc, exc_info=True)

    due_tasks, due_reminders = _claim_due_entries()

    dispatched: list[tuple[str, int]] = []
    errors: list[str] = []
    reminders_sent = 0

    # Dispatch reminders (no LLM)
    if due_reminders:
        await _dispatch_reminders(due_reminders, platform, default_chat_id=default_chat_id)
        reminders_sent = len(due_reminders)

    # Dispatch agent tasks (one-shot / periodic)
    if due_tasks:
        task_dispatched, task_errors = await _dispatch_agent_tasks(due_tasks, backend, platform)
        dispatched.extend(task_dispatched)
        errors.extend(task_errors)

    # Dispatch continuous task steps (checked independently of the claim system)
    try:
        cont_dispatched, cont_errors = await _handle_continuous_entries(backend, platform)
        dispatched.extend(cont_dispatched)
        errors.extend(cont_errors)
    except Exception as exc:
        log.error("Continuous task handling failed: %s", exc, exc_info=True)

    return {
        "dispatched": dispatched,
        "errors": errors,
        "reminders_sent": reminders_sent,
    }


# ── Migration from legacy formats ────────────────────────────────────────────


def _get_last_run_from_log(task_name: str) -> float | None:
    """Get timestamp of last OK/DISPATCHED run from bot.log (migration only)."""
    if not LOG_FILE.exists():
        return None

    last_time = None
    pattern = re.compile(
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] %s — (OK|DISPATCHED)' % re.escape(task_name)
    )

    for line in LOG_FILE.read_text().splitlines():
        match = pattern.search(line)
        if match:
            try:
                dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
                last_time = dt.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
    return last_time


def _parse_legacy_tasks_md() -> list[dict]:
    """Parse tasks.md into a list of task dicts (migration only)."""
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


# Keep parse_tasks as an alias for backward compatibility (used by /doupdate)
parse_tasks = _parse_legacy_tasks_md


def migrate_to_unified_queue() -> int:
    """Migrate from legacy formats (tasks.md, timed_queue.json, reminders.json)
    into the unified queue.json. Idempotent: skips if queue.json already exists.

    Returns the number of entries migrated.
    """
    if QUEUE_FILE.exists():
        log.info("queue.json already exists — skipping migration")
        return 0

    unified: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Migrate periodic tasks from tasks.md
    legacy_tasks = _parse_legacy_tasks_md()
    for task in legacy_tasks:
        if not task["enabled"]:
            continue
        if task["type"] in ("one-shot", "interactive"):
            continue

        freq = task["frequency"]
        interval = FREQUENCY_SECONDS.get(freq)
        if not interval:
            continue

        # Compute next_run based on last run from log
        last_run_ts = _get_last_run_from_log(task["name"])
        if last_run_ts is not None:
            last_run_dt = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
            next_run = _next_run_after(last_run_dt, interval)
        else:
            next_run = datetime.now(timezone.utc)  # Run immediately

        unified.append({
            "id": str(_uuid.uuid4()),
            "name": task["name"],
            "agent_file": task["agent_file"],
            "type": "periodic",
            "interval_seconds": interval,
            "next_run": next_run.isoformat(),
            "status": "pending",
            "model": task["model"],
            "thread_id": task["thread_id"],
            "description": task["description"],
            "created_at": now_iso,
            "migrated_from": "tasks.md",
        })
        log.info("Migrated periodic task '%s' (every %ds)", task["name"], interval)

    # 2. Migrate from timed_queue.json
    if TIMED_QUEUE_FILE.exists():
        try:
            timed_entries = json.loads(TIMED_QUEUE_FILE.read_text())
            if isinstance(timed_entries, list):
                for entry in timed_entries:
                    if entry.get("status") in ("pending", "dispatching"):
                        migrated_entry = dict(entry)
                        migrated_entry.pop("migrated_from_tasks_md", None)
                        migrated_entry["migrated_from"] = "timed_queue.json"
                        # Reset dispatching entries to pending
                        if migrated_entry.get("status") == "dispatching":
                            migrated_entry["status"] = "pending"
                            _clear_claim(migrated_entry)
                        unified.append(migrated_entry)
                        log.info(
                            "Migrated timed task '%s' (%s)",
                            entry.get("name"), entry.get("type"),
                        )
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to read timed_queue.json for migration: %s", exc)

    # 3. Migrate from reminders.json
    reminders_file = DATA_DIR / "reminders.json"
    if reminders_file.exists():
        try:
            reminders = json.loads(reminders_file.read_text())
            if isinstance(reminders, list):
                for entry in reminders:
                    if entry.get("status") in ("pending", "sending"):
                        migrated_entry = dict(entry)
                        migrated_entry["type"] = "reminder"
                        migrated_entry["migrated_from"] = "reminders.json"
                        if migrated_entry.get("status") == "sending":
                            migrated_entry["status"] = "pending"
                            _clear_claim(migrated_entry)
                        unified.append(migrated_entry)
                        log.info("Migrated reminder %s", entry.get("id"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to read reminders.json for migration: %s", exc)

    if not unified:
        # Write empty queue so we don't re-run migration
        _save_queue_unlocked(unified)
        log.info("Migration: no entries to migrate, wrote empty queue.json")
        return 0

    _save_queue_unlocked(unified)
    log.info("Migration complete: %d entries written to queue.json", len(unified))

    # Rename old files to .migrated backups
    for old_file in [TASKS_FILE, TIMED_QUEUE_FILE, reminders_file]:
        if old_file.exists():
            backup = old_file.with_suffix(old_file.suffix + ".migrated")
            try:
                old_file.rename(backup)
                log.info("Backed up %s -> %s", old_file.name, backup.name)
            except OSError as exc:
                log.warning("Could not rename %s: %s", old_file, exc)

    return len(unified)
