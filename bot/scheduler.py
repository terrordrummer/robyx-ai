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
The claim system prevents double-dispatch of one-shot and periodic
entries even on concurrent access.

Continuous tasks follow a different model: their source of truth is
the per-task ``data/continuous/<name>/state.json`` file (not the queue
entry), and the claim system does not apply. Dispatch safety comes from
the per-task lock file written alongside state (see
``scheduled_delivery.py``), and orphan recovery reconciles state back
to ``failed`` when the scheduler sees ``status="running"`` but the lock
file is gone (i.e. the subprocess crashed without clean teardown).

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
import sys
import threading
import time
import uuid as _uuid
from enum import Enum
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # POSIX only; on Windows we fall back to thread-lock-only.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from ai_backend import AIBackend, get_or_create_backend
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
from maintenance import MaintenanceActiveError, get_maintenance_gate
from model_preferences import resolve_model_preference
from persistence_recovery import (
    PersistenceUnavailableError,
    guard_json_write,
    load_json_with_recovery,
    recovery_marker_path,
)
from scheduled_delivery import start_task_delivery_watch
from task_scope import TaskScope, attach_scope, scope_from_record
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


class QueueUnavailableError(PersistenceUnavailableError):
    """The queue is corrupt and no verified recovery copy is available."""


def valid_queue_payload(value: Any) -> bool:
    """Validate the complete semantic shape of live and snapshot queues.

    Legacy records may omit ``workspace_scope`` so authoritative lifecycle
    lookup can migrate them under its stricter ambiguity rules. A present
    scope, however, must always be canonical. Structurally valid but empty or
    unknown entry objects are corruption, not an empty queue.
    """
    if not isinstance(value, list):
        return False

    def _text(raw: Any) -> bool:
        return isinstance(raw, str) and bool(raw.strip())

    def _timestamp(raw: Any) -> bool:
        if not _text(raw):
            return False
        try:
            datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return False
        return True

    for entry in value:
        if not isinstance(entry, dict):
            return False
        entry_type = entry.get("type")
        if entry_type not in {"reminder", "one-shot", "periodic", "continuous"}:
            return False
        if not _text(entry.get("status")):
            return False
        try:
            if scope_from_record(entry) is not None:
                pass
        except (TypeError, ValueError):
            return False

        if entry_type == "reminder":
            if not (
                _text(entry.get("id"))
                and isinstance(entry.get("message"), str)
                and bool(entry["message"].strip())
                and _timestamp(entry.get("fire_at"))
            ):
                return False
            continue

        if not _text(entry.get("name")):
            return False
        try:
            validate_task_name(entry["name"])
        except (TypeError, ValueError):
            return False
        # Old unified-queue generations did not persist every operational
        # field consistently. Keep those records readable for the explicit
        # migration/repair paths, but require a known type, status and stable
        # task name. The public enqueue APIs below enforce the complete modern
        # shape before any new record can be written.
    return True


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
    try:
        result = load_json_with_recovery(
            QUEUE_FILE,
            data_dir=DATA_DIR,
            validator=valid_queue_payload,
            kind="scheduler queue",
            logger=log,
        )
    except PersistenceUnavailableError as exc:
        raise QueueUnavailableError(str(exc)) from exc
    if result.status == "missing":
        return []
    if result.status == "recovered":
        log.critical(
            "Scheduler queue recovered from %s; inspect quarantined data and "
            "reconcile work created after the snapshot",
            result.snapshot,
        )
    return result.value


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
    if not valid_queue_payload(entries):
        raise ValueError("refusing to persist a malformed scheduler queue")
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
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
        # Public replacement is still a mutation: validate/recover the current
        # file first so callers cannot overwrite a corrupt queue with an
        # apparently-clean list derived from stale or incomplete data.
        try:
            guard_json_write(
                QUEUE_FILE,
                data_dir=DATA_DIR,
                validator=valid_queue_payload,
                kind="scheduler queue",
                logger=log,
            )
        except PersistenceUnavailableError as exc:
            raise QueueUnavailableError(str(exc)) from exc
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


def add_task(
    task: dict,
    *,
    scope: TaskScope | None = None,
    allow_legacy_unscoped: bool = False,
) -> None:
    """Append a one-shot, periodic, or continuous task to the queue atomically.

    Required: ``name``, ``agent_file``, ``type``, ``scheduled_at`` (one-shot).
    """
    queued = dict(task)
    if scope is None:
        if not allow_legacy_unscoped:
            raise ValueError("workspace scope is required for new scheduler tasks")
    else:
        attach_scope(queued, scope)
    queued["name"] = validate_task_name(queued.get("name", ""))

    task_type = queued.get("type", "one-shot")
    if task_type not in {"one-shot", "periodic", "continuous"}:
        raise ValueError("unsupported scheduler task type: %r" % task_type)

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
    elif task_type == "periodic":
        interval = queued.get("interval_seconds")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise ValueError("interval_seconds must be positive for periodic tasks")
        queued["next_run"] = validate_one_shot_scheduled_at(
            queued.get("next_run"),
            label="periodic tasks",
        )
    elif (
        not allow_legacy_unscoped
        and (
            not isinstance(queued.get("state_file"), str)
            or not queued["state_file"].strip()
        )
    ):
        raise ValueError("state_file is required for continuous tasks")

    queued.setdefault("id", str(_uuid.uuid4()))
    queued.setdefault("status", "pending")
    queued.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    with _queue_mutex():
        entries = _load_queue_unlocked()
        if task_type == "continuous":
            # A deleted task keeps a canceled queue tombstone so repeated
            # delete remains distinguishable from never-created.  Once a new
            # generation with the same name has passed creation validation,
            # replace only those old continuous tombstones; otherwise both
            # entries resolve through the same new state.json and lifecycle
            # lookup becomes spuriously ambiguous.  Active/resumable entries
            # and same-name entries of other task types are untouched.
            entries = [
                entry for entry in entries
                if not (
                    entry.get("type") == "continuous"
                    and entry.get("name") == queued["name"]
                    and entry.get("status") == "canceled"
                )
            ]
        entries.append(queued)
        _save_queue_unlocked(entries)


def add_reminder(
    entry: dict,
    *,
    scope: TaskScope | None = None,
    allow_legacy_unscoped: bool = False,
) -> None:
    """Append a reminder entry to the queue atomically.

    Required: ``fire_at``, ``message``. Optional: ``chat_id``, ``thread_id``.
    """
    queued = dict(entry)
    if scope is None:
        if not allow_legacy_unscoped:
            raise ValueError("workspace scope is required for new reminders")
    else:
        attach_scope(queued, scope)
    message = queued.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required for reminders")
    queued["fire_at"] = validate_one_shot_scheduled_at(
        queued.get("fire_at"),
        label="reminders",
    )
    queued["type"] = "reminder"
    queued.setdefault("id", str(_uuid.uuid4()))
    queued.setdefault("status", "pending")
    queued.setdefault("attempts", 0)
    queued.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    with _queue_mutex():
        entries = _load_queue_unlocked()
        entries.append(queued)
        _save_queue_unlocked(entries)


def rollback_continuous_creation(name: str, previous_entries: list[dict]) -> None:
    """Restore one task name's queue slice after a failed create transaction.

    The queue mutex keeps unrelated scheduler/lifecycle writes intact; this is
    deliberately narrower than restoring a whole pre-create queue snapshot.
    """
    with _queue_mutex():
        entries = [
            entry for entry in _load_queue_unlocked()
            if not (
                entry.get("type") == "continuous"
                and entry.get("name") == name
            )
        ]
        entries.extend(dict(entry) for entry in previous_entries)
        _save_queue_unlocked(entries)


def repoint_continuous_topic(name: str, new_thread_id: Any) -> None:
    """Atomically update the active queue route for a recreated topic."""
    with _queue_mutex():
        entries = _load_queue_unlocked()
        matched = False
        for entry in entries:
            if (
                entry.get("type") == "continuous"
                and entry.get("name") == name
                and entry.get("status") != "canceled"
            ):
                entry["thread_id"] = str(new_thread_id)
                matched = True
        if not matched:
            raise RuntimeError("active queue entry missing for continuous task '%s'" % name)
        _save_queue_unlocked(entries)


def set_continuous_workspace_scope(name: str, workspace_scope: dict) -> None:
    """Persist a continuous task's immutable ownership scope atomically.

    Used by the one-time legacy migration after state-level ownership has
    been proven. A conflicting modern scope is an integrity error and is
    never overwritten.
    """
    from task_scope import TaskScope, scope_from_record

    canonical = TaskScope.from_dict(workspace_scope)
    with _queue_mutex():
        entries = _load_queue_unlocked()
        matched = False
        for entry in entries:
            if (
                entry.get("type") != "continuous"
                or entry.get("name") != name
                or entry.get("status") == "canceled"
            ):
                continue
            existing = scope_from_record(entry)
            if existing is not None and existing != canonical:
                raise RuntimeError(
                    "continuous queue scope conflict for task '%s'" % name
                )
            entry["workspace_scope"] = canonical.to_dict()
            matched = True
        if not matched:
            raise RuntimeError(
                "active queue entry missing for continuous task '%s'" % name
            )
        _save_queue_unlocked(entries)


def set_queue_entry_workspace_scope(entry_id: str, workspace_scope: dict) -> None:
    """Atomically migrate one legacy non-continuous queue entry."""
    from task_scope import TaskScope, scope_from_record

    canonical = TaskScope.from_dict(workspace_scope)
    with _queue_mutex():
        entries = _load_queue_unlocked()
        matched = False
        for entry in entries:
            if str(entry.get("id") or "") != str(entry_id):
                continue
            existing = scope_from_record(entry)
            if existing is not None and existing != canonical:
                raise RuntimeError(
                    "queue scope conflict for entry '%s'" % entry_id
                )
            entry["workspace_scope"] = canonical.to_dict()
            matched = True
            break
        if not matched:
            raise RuntimeError("queue entry '%s' is missing" % entry_id)
        _save_queue_unlocked(entries)


async def drain_and_cancel_continuous_task(
    task_name: str,
    *,
    reason: str = "workspace closed",
    drain_timeout_seconds: int | None = None,
    terminate_immediately: bool = False,
    journal_events: bool = True,
) -> dict:
    """Spec 006 FR-021 — drain-on-close with a bounded window.

    If the continuous task ``task_name`` has a running subprocess, wait
    up to ``drain_timeout_seconds`` (or the task's per-state override,
    or ``DRAIN_TIMEOUT_DEFAULT_SECONDS``) for it to exit naturally. If
    the timeout elapses first, SIGTERM + 5s grace + SIGKILL, then
    journal ``drain_timeout``.

    Lifecycle operations pass ``terminate_immediately=True``: in that mode
    SIGTERM is sent first, followed by a bounded grace window (at most five
    seconds) and SIGKILL if necessary. Workspace-close keeps the historical
    drain-first behaviour. The queue entry is canceled *before* either flow,
    preventing a new scheduler cycle from dispatching while the drain waits.

    ``journal_events=False`` is used by lifecycle transitions because their
    public contract requires a single transition event; workspace-close keeps
    the lower-level drain events enabled.

    Returns drain/termination outcome flags and the elapsed wait duration.
    """
    import asyncio as _asyncio
    from process import is_pid_alive
    from runtime_supervisor import get_runtime_supervisor

    try:
        from config import DRAIN_TIMEOUT_DEFAULT_SECONDS as _default_drain
    except Exception:
        _default_drain = 3600

    from continuous import load_state, save_state, state_file_path

    # Resolve effective drain window.
    state_path = state_file_path(task_name)
    state = load_state(state_path)
    effective = drain_timeout_seconds
    if effective is None and state is not None:
        effective = state.get("drain_timeout_seconds")
    if effective is None:
        effective = _default_drain
    effective = max(1, int(effective))

    # Stop future dispatches before yielding to the event loop.  Canceling at
    # the end left a window where a concurrent scheduler cycle could spawn a
    # new step while this coroutine was waiting for the previous one.
    cancel_task_by_name(task_name, reason=reason)

    if journal_events:
        _journal_scheduler_event(
            task_name=task_name,
            event_type="drain_started",
            outcome="ok",
            payload={"timeout_seconds": effective, "reason": reason},
        )

    # Locate the running subprocess via lock file.
    lock_file = DATA_DIR / task_name / "lock"
    pid: int | None = None
    lock_identity: dict[str, object] | None = None
    if lock_file.exists():
        pid_raw, _, lock_identity = _parse_lock_record(lock_file.read_text())
        pid = pid_raw

    waited = 0.0
    timed_out = False
    drained = False
    terminated = False
    supervisor = get_runtime_supervisor()
    if (
        pid is not None
        and is_pid_alive(pid)
        and not supervisor.process_tree_alive(pid)
        and not _lock_process_identity_matches(pid, lock_identity)
    ):
        log.error(
            "Refusing to drain PID %d for '%s': persisted process identity "
            "is missing or changed",
            pid,
            task_name,
        )
        lock_file.unlink(missing_ok=True)
        pid = None
    process_found = pid is not None and (
        supervisor.process_tree_alive(pid) or is_pid_alive(pid)
    )

    if process_found and pid is not None:
        def _tree_alive() -> bool:
            return supervisor.process_tree_alive(pid) or is_pid_alive(pid)

        if state is not None and reason == "workspace closed":
            # The delivery watcher reads this just before rendering.  Persist
            # it before waiting so a naturally-completing step is visibly
            # distinguished from an ordinary step completion.
            state["delivery_state_override"] = "workspace_closed"
            state["workspace_closed_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state_path, state)

        # Explicit lifecycle operations terminate now; workspace close allows
        # natural completion for the configured drain window.
        if terminate_immediately:
            start = time.monotonic()
            drained = await supervisor.terminate_process_by_pid(
                pid,
                grace_seconds=min(float(effective), 5.0),
            )
            waited = time.monotonic() - start
            terminated = True
            timed_out = not drained
        else:
            start = time.monotonic()
            while waited < float(effective):
                if not _tree_alive():
                    drained = True
                    break
                await _asyncio.sleep(min(1.0, max(0.0, float(effective) - waited)))
                waited = time.monotonic() - start
            if not _tree_alive():
                drained = True

        if not drained and not terminate_immediately and _tree_alive():
            timed_out = True
            if state is not None and reason == "workspace closed":
                state = load_state(state_path) or state
                state["delivery_state_override"] = "drain_timeout"
                save_state(state_path, state)
            terminated = True
            drained = await supervisor.terminate_process_by_pid(
                pid,
                grace_seconds=5.0,
            )
            if journal_events:
                _journal_scheduler_event(
                    task_name=task_name,
                    event_type="drain_timeout",
                    outcome="killed",
                    payload={"pid": pid, "timeout_seconds": effective},
                )

        # Await the delivery watcher too: closing a workspace must not archive
        # its parent and return while final task output is still in flight.
        watcher = supervisor.get_named_task("scheduled_delivery:%s" % pid)
        if watcher is not None:
            try:
                await _asyncio.wait_for(_asyncio.shield(watcher), timeout=5.0)
            except _asyncio.TimeoutError:
                log.error("Delivery watcher for '%s' did not drain", task_name)

        # The watcher normally removes the lock.  Do it here only after the
        # complete tree is gone; retaining it on failure keeps recovery
        # fail-closed instead of allowing a duplicate dispatch.
        if drained and not _tree_alive():
            lock_file.unlink(missing_ok=True)

    if journal_events:
        _journal_scheduler_event(
            task_name=task_name,
            event_type="drain_completed",
            outcome=(
                "drained" if drained
                else ("timeout" if timed_out else "not_running")
            ),
            payload={"waited_seconds": round(waited, 2)},
        )

    return {
        "drained": drained,
        "timeout": timed_out,
        "terminated": terminated,
        "process_found": process_found,
        "tree_stopped": not process_found or drained,
        "waited_seconds": round(waited, 2),
    }


def cancel_tasks_for_agent_file(
    agent_file: str,
    *,
    reason: str = "workspace closed",
    transaction_id: str | None = None,
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
            if transaction_id is not None:
                entry["cancel_transaction_id"] = transaction_id
            canceled += 1

        if canceled:
            _save_queue_unlocked(entries)
            log.info(
                "Canceled %d pending task(s) for %s (%s)",
                canceled, agent_file, reason,
            )

    return canceled


def restore_tasks_canceled_by_transaction(
    agent_file: str,
    transaction_id: str,
    *,
    exclude_names: set[str] | None = None,
) -> int:
    """Compensate only cancellation fields still owned by one close attempt."""
    if not agent_file or not transaction_id:
        return 0
    excluded = exclude_names or set()
    with _queue_mutex():
        entries = _load_queue_unlocked()
        restored = 0
        changed = False
        for entry in entries:
            if (
                entry.get("agent_file") != agent_file
                or entry.get("status") != "canceled"
                or entry.get("cancel_transaction_id") != transaction_id
            ):
                continue
            if entry.get("name") in excluded:
                entry.pop("cancel_transaction_id", None)
                changed = True
                continue
            entry["status"] = "pending"
            entry.pop("canceled_at", None)
            entry.pop("canceled_reason", None)
            entry.pop("cancel_transaction_id", None)
            restored += 1
            changed = True
        if changed:
            _save_queue_unlocked(entries)
    return restored


# Alias for backward compatibility
cancel_tasks_for_agent = cancel_tasks_for_agent_file


def cancel_task_by_name(name: str, *, reason: str = "stopped by user") -> bool:
    """Cancel pending/active queue entries whose ``name`` matches exactly.

    Used by the lifecycle-macro handlers (spec 005, US2) to stop a
    periodic, one-shot, or continuous task by user request. Returns True
    if at least one entry was canceled. Reminders do not carry a ``name``
    field so this helper skips them — callers that need reminder
    cancellation should match by ``id`` via their own flow.
    """
    if not name:
        return False

    canceled = 0
    canceled_at = datetime.now(timezone.utc).isoformat()
    with _queue_mutex():
        entries = _load_queue_unlocked()
        for entry in entries:
            if entry.get("name") != name:
                continue
            if entry.get("status") in ("canceled", "dispatched", "completed"):
                continue
            entry["status"] = "canceled"
            entry["canceled_at"] = canceled_at
            entry["canceled_reason"] = reason
            canceled += 1
        if canceled:
            _save_queue_unlocked(entries)
            log.info(
                "Canceled %d queue entr%s for name=%r (%s)",
                canceled, "y" if canceled == 1 else "ies", name, reason,
            )
    return canceled > 0


def reactivate_continuous_task_by_name(name: str) -> bool:
    """Return a stopped continuous queue entry to ``pending`` on resume.

    Stop/complete/delete cancel queue entries before draining so no new step
    can race the lifecycle transition.  Resume is the sole operation allowed
    to reverse that cancellation, and only for continuous tasks whose state
    machine has independently accepted the transition.
    """
    if not name:
        return False

    reactivated = 0
    with _queue_mutex():
        entries = _load_queue_unlocked()
        for entry in entries:
            if entry.get("name") != name or entry.get("type") != "continuous":
                continue
            if entry.get("status") != "canceled":
                continue
            entry["status"] = "pending"
            entry.pop("canceled_at", None)
            entry.pop("canceled_reason", None)
            reactivated += 1
        if reactivated:
            _save_queue_unlocked(entries)
            log.info(
                "Reactivated %d continuous queue entr%s for name=%r",
                reactivated,
                "y" if reactivated == 1 else "ies",
                name,
            )
    return reactivated > 0


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
    """Advance ``run_at`` by full intervals until it is strictly in the future.

    Invariant: a periodic task with N missed intervals fires **once** on
    recovery (the scheduler dispatches the currently-due instance), and its
    ``next_run`` is then advanced past ``now``. N-1 missed instances are
    intentionally skipped to avoid a thundering-herd on resume.
    """
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

        # Spec 005: prefix reminders with the 🔔 marker via the single
        # delivery-layer chokepoint. Reminders today carry no per-item
        # "name" field — fall back to a friendly label so the marker
        # remains readable without exposing internal UUIDs.
        from scheduled_delivery import format_delivery_message
        reminder_label = reminder.get("name") or "promemoria"
        marked_text = format_delivery_message(
            "reminder", reminder_label, reminder["message"],
        )

        try:
            sent = await asyncio.wait_for(
                platform.send_message(
                    chat_id=chat_id,
                    text=marked_text,
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
        except Exception as e:
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
    env_overrides: dict[str, str] | None = None,
    owner: str = "scheduled",
) -> asyncio.subprocess.Process:
    """Run an AI CLI subprocess, redirecting stdout+stderr to ``output_log``.

    Centralises the boilerplate shared by the one-shot/periodic path and
    the continuous-task path: open the log, launch the CLI, pipe the
    prompt payload into stdin (tolerating both sync and async
    ``write`` / ``close`` variants that asyncio StreamWriter exposes).
    """
    child_env = None
    if env_overrides:
        child_env = os.environ.copy()
        child_env.update(env_overrides)

    proc = None
    try:
        from runtime_supervisor import get_runtime_supervisor

        supervisor = get_runtime_supervisor()
        supervisor.reject_if_closing()
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
                env=child_env,
                start_new_session=sys.platform != "win32",
            )
            supervisor.track_process(
                proc, owner=owner, process_group=sys.platform != "win32",
            )
            if stdin_payload is not None and proc.stdin is not None:
                write_result = proc.stdin.write(stdin_payload)
                if inspect.isawaitable(write_result):
                    await write_result
                await proc.stdin.drain()
                close_result = proc.stdin.close()
                if inspect.isawaitable(close_result):
                    await close_result
    except BaseException:
        if proc is not None:
            await _terminate_uncommitted_subprocess(proc)
        raise
    return proc


async def _terminate_uncommitted_subprocess(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Terminate a process spawned for a dispatch that lost its state race.

    The process has not yet been recorded in state or in the lock file, so the
    normal drain path cannot find it.  Waiting here guarantees that a lifecycle
    transition which canceled the queue during ``create_subprocess_exec`` does
    not leave an untracked agent running in the workspace.
    """
    from runtime_supervisor import get_runtime_supervisor

    await get_runtime_supervisor().terminate_process(
        proc, grace_seconds=grace_seconds,
    )


def _write_lock_file(lock_file: Path, pid: int) -> None:
    """Write the initial lock file with an optional process identity.

    Line 1: ``<pid>``
    Line 2: ``<iso8601_heartbeat_ts>``
    Line 3: compact JSON process identity (start fingerprint, executable,
    command name, and process group). Older two-line readers remain valid.

    The heartbeat timestamp is refreshed periodically by the subprocess
    via :func:`refresh_heartbeat` so stale-lock detection can distinguish
    "subprocess alive but slow" from "subprocess crashed/SIGKILL'd".
    """
    from process import get_process_identity_sync

    now_str = datetime.now(timezone.utc).isoformat()
    identity = get_process_identity_sync(pid)
    suffix = ""
    if identity is not None:
        suffix = json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    # Pre-spec-006 single-line format kept as a subline-safe prefix: some
    # readers that only look at line 1 still see the pid.
    lock_file.write_text("%d\n%s\n%s" % (pid, now_str, suffix))


def refresh_heartbeat(lock_file: Path, pid: int) -> None:
    """Atomic heartbeat refresh preserving the original process identity
    via a temp file + ``os.replace`` so concurrent ``check_lock`` readers
    never see a torn file.

    Used by the subprocess-side heartbeat loop (spec 006 FR-019).
    """
    now_str = datetime.now(timezone.utc).isoformat()
    identity = None
    try:
        recorded_pid, _, identity = _parse_lock_record(lock_file.read_text())
        if recorded_pid != pid:
            return
    except OSError:
        pass
    suffix = ""
    if identity is not None:
        suffix = json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    tmp = lock_file.with_suffix(".lock.tmp-%d" % pid)
    try:
        tmp.write_text("%d\n%s\n%s" % (pid, now_str, suffix))
        os.replace(str(tmp), str(lock_file))
    except OSError:
        # Best-effort — failure to refresh is non-fatal; scheduler will
        # eventually detect the stale heartbeat and recover. Cleanup.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class LockStatus(str, Enum):
    """Outcome of ``check_lock`` — spec 006."""
    ALIVE = "alive"
    STALE_DEAD_PID = "stale_dead_pid"
    STALE_ZOMBIE = "stale_zombie"
    PID_REUSED = "pid_reused"
    MISSING = "missing"


def _parse_lock_record(
    content: str,
) -> tuple[int | None, datetime | None, dict[str, object] | None]:
    """Parse pid, heartbeat, and optional durable process identity.

    Accepts three formats for backward compat:
    * Legacy single-line ``<pid> <iso_ts>`` (space-separated).
    * Legacy pid-only ``<pid>``.
    * Spec 006 two-line ``<pid>\\n<iso_ts>\\n``.
    * Hardened three-line record with a JSON identity on line 3.

    Returns ``(None, None, None)`` on unparseable content.
    """
    content = content.strip()
    if not content:
        return None, None, None
    pid: int | None = None
    ts: datetime | None = None
    identity: dict[str, object] | None = None

    if "\n" in content:
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            return None, None, None
        try:
            pid = int(lines[0])
        except ValueError:
            return None, None, None
        if len(lines) >= 2:
            try:
                ts = datetime.fromisoformat(lines[1])
            except ValueError:
                ts = None
        if len(lines) >= 3:
            try:
                candidate = json.loads(lines[2])
                if isinstance(candidate, dict):
                    identity = candidate
            except (TypeError, ValueError):
                identity = None
    else:
        parts = content.split()
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            return None, None, None
        if len(parts) >= 2:
            try:
                ts = datetime.fromisoformat(parts[1])
            except ValueError:
                ts = None

    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return pid, ts, identity


def _parse_lock_content(content: str) -> tuple[int | None, datetime | None]:
    """Backward-compatible two-field view of :func:`_parse_lock_record`."""
    pid, heartbeat_ts, _ = _parse_lock_record(content)
    return pid, heartbeat_ts


def _lock_process_identity_matches(
    pid: int,
    identity: dict[str, object] | None,
) -> bool:
    """Verify a persisted lock owner without trusting PID/name alone."""
    from process import process_identity_matches

    if identity is not None:
        return process_identity_matches(pid, identity)
    try:
        from runtime_supervisor import get_runtime_supervisor
        if get_runtime_supervisor().process_tree_alive(pid):
            return True
        import orphan_tracker
        return orphan_tracker.registered_identity_matches(pid)
    except Exception:
        return False


async def check_lock_status(task_name: str) -> tuple[LockStatus, int | None]:
    """Spec 006 stale-aware lock check.

    Returns the ``LockStatus`` and — when present and parseable — the pid
    recorded in the lock file. Callers deciding whether to reclaim should
    prefer this over the legacy bool-returning :func:`check_lock`.

    Recovery semantics:
      * ``ALIVE`` — subprocess is running AND heartbeat is fresh (or the
        lock is in legacy pid-only format and the pid is alive). Skip
        this task for the current scheduler cycle.
      * ``STALE_DEAD_PID`` — pid is no longer on the OS. Scheduler may
        delete the lock and reclaim.
      * ``STALE_ZOMBIE`` — pid exists but heartbeat is older than the
        configured threshold. Scheduler attempts SIGTERM + grace +
        SIGKILL before deleting the lock.
      * ``MISSING`` — no lock file. Task is not currently running.
    """
    try:
        safe_name = validate_task_name(task_name)
    except ValueError as exc:
        log.error("Invalid task name for lock check %r: %s", task_name, exc)
        return LockStatus.MISSING, None

    lock_file = DATA_DIR / safe_name / "lock"
    if not lock_file.exists():
        return LockStatus.MISSING, None

    try:
        content = lock_file.read_text()
    except OSError as exc:
        log.warning(
            "check_lock_status: read error on %s: %s — treating as missing",
            lock_file, exc,
        )
        return LockStatus.MISSING, None

    pid, heartbeat_ts, identity = _parse_lock_record(content)
    if pid is None:
        # Unparseable lock — treat as missing so we don't deadlock.
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
        return LockStatus.MISSING, None

    from process import is_pid_alive

    if not is_pid_alive(pid):
        return LockStatus.STALE_DEAD_PID, pid

    # A live PID and familiar command name are not ownership proofs. Detect
    # PID reuse before trusting even a fresh heartbeat.
    if identity is not None and not _lock_process_identity_matches(pid, identity):
        return LockStatus.PID_REUSED, pid

    # Pid alive. If heartbeat info is absent (legacy format), trust the
    # pid — earlier Robyx never deadlocked on pid-alive-but-slow. If
    # present, enforce the stale threshold.
    if heartbeat_ts is not None:
        try:
            from config import LOCK_STALE_THRESHOLD_SECONDS as _threshold
        except Exception:
            _threshold = 300
        age = (datetime.now(timezone.utc) - heartbeat_ts).total_seconds()
        if age > _threshold:
            # Legacy two-line locks are signal-safe only when the current
            # in-memory supervisor or durable orphan registry proves identity.
            if not _lock_process_identity_matches(pid, identity):
                return LockStatus.PID_REUSED, pid
            return LockStatus.STALE_ZOMBIE, pid

    return LockStatus.ALIVE, pid


# ── Dispatch: agent tasks (one-shot / periodic) ─────────────────────────────


def _resolve_entry_backend(entry: dict, default: AIBackend) -> AIBackend:
    """Return the backend to spawn this queue entry against.

    A per-entry ``backend`` field (set by ``[CREATE_WORKSPACE ... backend="…"]``
    when the workspace was created) wins over the global default. Lookup
    failures (unknown name / missing CLI) log and fall back to *default*
    so a misconfigured queue entry never silently halts the dispatch loop.
    """
    requested = entry.get("backend")
    if not requested:
        return default
    try:
        return get_or_create_backend(requested)
    except (ValueError, FileNotFoundError) as exc:
        log.error(
            "Queue entry '%s' requested backend '%s' but it is unavailable (%s) — "
            "falling back to global default",
            entry.get("name"), requested, exc,
        )
        return default


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
    backend = _resolve_entry_backend(task, backend)
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

    silence_policy = (
        "OUTPUT POLICY (silence by default):\n"
        "Notify the user only when there is something actionable: an anomaly,\n"
        "a deadline, an event, a question that needs a human decision, or a\n"
        "concrete result the user explicitly asked for. Do NOT emit\n"
        "'all clear' status reports, system snapshots (disk/CPU/memory/lock\n"
        "files), or recap tables when nothing requires attention. If the run\n"
        "has nothing actionable to report, your final response must be\n"
        "exactly `[SILENT]` on its own line and nothing else — the\n"
        "delivery layer suppresses it.\n"
        "Failures and errors are never silent: always report them."
    )

    if prompt_override:
        full_prompt = (
            "You are a scheduled sub-agent for task '%s'.\n\n"
            "Your specific task for this run:\n%s\n\n"
            "Context from agent instructions:\n---\n%s\n---\n\n"
            "%s\n\n"
            "%s\n\n"
            "WHEN DONE (success or failure):\n"
            "1. Append to %s:\n"
            "   [current date and time] %s -- OK -- <brief summary>\n"
            "   (use ERROR instead of OK if you failed)\n"
            "2. Delete your lock file: rm -f %s\n"
            "3. Always delete the lock file, even on error."
        ) % (task_name, prompt_override, agent_instructions, memory_ctx,
             silence_policy, LOG_FILE, task_name, lock_file)
    else:
        full_prompt = (
            "You are a scheduled sub-agent for task '%s'.\n\n"
            "Execute the following instructions completely and autonomously:\n\n"
            "---\n%s\n---\n\n"
            "%s\n\n"
            "%s\n\n"
            "WHEN DONE (success or failure):\n"
            "1. Append to %s:\n"
            "   [current date and time] %s -- OK -- <brief summary>\n"
            "   (use ERROR instead of OK if you failed)\n"
            "2. Delete your lock file: rm -f %s\n"
            "3. Always delete the lock file, even on error."
        ) % (task_name, agent_instructions, memory_ctx,
             silence_policy, LOG_FILE, task_name, lock_file)

    invocation = backend.build_spawn_invocation(
        prompt=full_prompt,
        model=model,
        work_dir=runtime.work_dir,
    )
    cmd = invocation.argv
    stdin_payload = backend.spawn_stdin_payload(full_prompt)

    try:
        proc = await _spawn_ai_subprocess(
            cmd=cmd,
            stdin_payload=stdin_payload,
            output_log=output_log,
            work_dir=runtime.work_dir,
            env_overrides=invocation.env_overrides,
            owner="scheduled:%s" % task_name,
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


def _load_parent_workspace_instructions(state: dict) -> str:
    """Load the parent workspace's ``agents/<name>.md`` instructions.

    Spec 005 US5: the secondary step agent inherits the same workspace-
    level instructions as the primary so behaviour does not drift. When
    the parent workspace has no file yet (freshly-created workspace,
    missing migration, tests), we return a short placeholder so the
    template still renders cleanly.
    """
    parent_name = state.get("parent_workspace") or state.get("parent_workspace_name")
    if not parent_name:
        return "_(no parent workspace recorded for this task)_"
    try:
        from config import AGENTS_DIR
        path = AGENTS_DIR / ("%s.md" % parent_name)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        log.warning(
            "continuous: failed to load parent workspace instructions for %r: %s",
            parent_name, exc,
        )
    return "_(parent workspace instructions file not found)_"


def _load_plan_md_for_prompt(name: str) -> str:
    """Load ``data/continuous/<name>/plan.md`` for inclusion in the prompt.

    Spec 005 US5: tasks created post-0.23.0 always have a plan.md;
    migrated pre-0.23.0 tasks have one materialised by the v0_23_0
    migration. If nothing is found we still render a short placeholder
    so the step agent understands the absence is expected, not a bug.
    """
    try:
        from continuous import read_plan_md
        body = read_plan_md(name)
    except Exception as exc:
        log.warning(
            "continuous: failed to read plan.md for '%s': %s", name, exc,
        )
        body = None
    if not body:
        return (
            "_(no plan.md available for this task — refer to the Program "
            "section below for intent)_"
        )
    return body.strip()


def _maybe_demote_on_demand_awaiting_input(state: dict, name: str) -> bool:
    """Server-side enforcement of the ``on-demand`` checkpoint policy.

    A step agent running under ``on-demand`` has no legitimate reason to
    park its task in ``awaiting-input`` — the template is explicit about
    this. If it happens anyway (stale instructions, model misbehaviour,
    prompt injection) the task would stall forever because the scheduler
    skips ``awaiting-input`` entries. Auto-demote to ``pending`` so the
    loop keeps running; the policy violation is logged.

    Returns True if the state was mutated (caller should persist it).
    """
    if state.get("status") != "awaiting-input":
        return False
    policy = (state.get("program") or {}).get("checkpoint_policy") or "on-demand"
    if policy != "on-demand":
        return False
    log.warning(
        "Continuous task '%s': on-demand policy violated "
        "(step parked in awaiting-input); auto-demoting to pending",
        name,
    )
    state["status"] = "pending"
    state.pop("awaiting_question", None)
    return True


async def _sync_continuous_topic_marker(
    state: dict,
    name: str,
    platform,
    *,
    actionable_event: str | None = None,
) -> bool:
    """Apply a state marker once, routing permanent failures to recovery."""
    if platform is None:
        return False
    from continuous import (
        save_state,
        state_file_path,
        unpin_awaiting_message,
        update_topic_state_marker,
    )
    if (
        state.get("status") not in ("awaiting_input", "awaiting-input")
        and state.get("awaiting_pinned_msg_id") is not None
    ):
        try:
            from config import CHAT_ID
            if await unpin_awaiting_message(state, platform, CHAT_ID):
                save_state(state_file_path(name), state)
        except Exception as exc:
            from messaging.base import TopicUnreachable
            if isinstance(exc, TopicUnreachable):
                from topic_recovery import recover_unreachable_topic
                await recover_unreachable_topic(
                    name,
                    platform,
                    reason=exc.reason or str(exc),
                    event=actionable_event,
                )
                return False
            raise
    if state.get("topic_marker_status") == state.get("status"):
        return False
    try:
        updated = await update_topic_state_marker(state, platform)
    except Exception as exc:
        from messaging.base import TopicUnreachable
        if not isinstance(exc, TopicUnreachable):
            raise
        from topic_recovery import recover_unreachable_topic
        await recover_unreachable_topic(
            name,
            platform,
            reason=exc.reason or str(exc),
            event=actionable_event,
        )
        return False
    if updated:
        state["topic_marker_status"] = state.get("status")
        save_state(state_file_path(name), state)
    elif state.get("dedicated_thread_id") is not None:
        from topic_recovery import recover_unreachable_topic
        await recover_unreachable_topic(
            name,
            platform,
            reason="topic marker update returned false",
            event=actionable_event,
        )
    return bool(updated)


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
        # Per-entry crash isolation: a corrupt state file, a history entry
        # that drifts from the documented schema, or any other unexpected
        # error must only affect THIS task. Pre-v0.24.3 a KeyError inside
        # build_step_context would bubble out of the whole loop and leave
        # every other continuous task undispatched on every scheduler tick.
        try:
            sf = entry.get("state_file")
            if not sf:
                sf = str(state_file_path(name))

            state = load_state(Path(sf))
            if state is None:
                log.warning("Continuous task '%s': state file missing at %s", name, sf)
                continue

            # Handle rate-limited tasks
            if state["status"] in ("rate_limited", "rate-limited"):
                if check_rate_limit_recovery(state):
                    previous_status = state["status"]
                    resume_task(state)
                    save_state(Path(sf), state)
                    await _sync_continuous_topic_marker(state, name, platform)
                    _journal_scheduler_event(
                        task_name=name,
                        event_type="rate_limit_recovered",
                        outcome="recovered",
                        payload={"prev_status": previous_status},
                    )
                    log.info("Continuous task '%s': rate limit recovered, resuming", name)
                else:
                    await _sync_continuous_topic_marker(state, name, platform)
                    continue

            # Server-side enforcement of on-demand checkpoint policy.
            if _maybe_demote_on_demand_awaiting_input(state, name):
                save_state(Path(sf), state)

            status_event = (
                "awaiting_input"
                if state["status"] in ("awaiting_input", "awaiting-input")
                else ("error" if state["status"] == "error" else None)
            )
            await _sync_continuous_topic_marker(
                state,
                name,
                platform,
                actionable_event=status_event,
            )

            # Skip if not ready.
            # Spec 006: accept both legacy (hyphen / "paused") and canonical
            # (underscore / "stopped") forms. Any of these means the task is
            # not currently eligible for dispatch.
            if state["status"] in (
                "completed", "deleted", "error",
                "stopped", "paused",
                "awaiting_input", "awaiting-input",
            ):
                continue

            # Spec 006 US4: stale-aware lock check. Decides between
            # ALIVE (skip this cycle), STALE_DEAD_PID / STALE_ZOMBIE
            # (clean the lock and run the orphan-backoff path), MISSING
            # (subprocess already exited — fall through to orphan path).
            if state["status"] == "running":
                lock_status, pid = await check_lock_status(name)
                if lock_status == LockStatus.ALIVE:
                    continue  # Subprocess still running and heartbeating

                # Recovery: remove the stale lock. STALE_ZOMBIE means the
                # pid is still alive but heartbeat died; attempt graceful
                # termination before unlinking.
                lock_file = DATA_DIR / name / "lock"
                if lock_status == LockStatus.STALE_ZOMBIE and pid is not None:
                    from runtime_supervisor import get_runtime_supervisor
                    stopped = await get_runtime_supervisor().terminate_process_by_pid(
                        pid,
                        grace_seconds=5.0,
                    )
                    if not stopped:
                        log.error(
                            "STALE_ZOMBIE tree termination failed for %s (pid=%d); "
                            "retaining lock",
                            name,
                            pid,
                        )
                        continue

                lock_file.unlink(missing_ok=True)
                _journal_scheduler_event(
                    task_name=name,
                    event_type="lock_recovered",
                    outcome=lock_status.value if lock_status != LockStatus.MISSING else "missing",
                    payload={"pid": pid},
                )

                # Feed into the orphan-backoff path (writes state; the
                # scheduler may re-dispatch on the next cycle if below
                # threshold, or escalate to incident).
                incident = _handle_continuous_orphan(state, name)
                save_state(Path(sf), state)
                if incident is not None:
                    await _sync_continuous_topic_marker(
                        state,
                        name,
                        platform,
                        actionable_event="task_death",
                    )
                    await _deliver_orphan_incident(
                        state,
                        name,
                        incident,
                        platform,
                    )
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

            # Spec 005 US5: secondary agent shares primary workspace knowledge.
            # Load (a) the parent workspace's agent instructions file and (b)
            # the task-specific plan.md; substitute both into the prompt so
            # behaviour stays consistent with the primary that set the task up.
            parent_instructions = _load_parent_workspace_instructions(state)
            plan_md_body = _load_plan_md_for_prompt(name)

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
                .replace("{{PARENT_WORKSPACE_INSTRUCTIONS}}", parent_instructions)
                .replace("{{PLAN_MD}}", plan_md_body)
                .replace("{{OBJECTIVE}}", program.get("objective", ""))
                .replace("{{SUCCESS_CRITERIA}}", criteria_text or "(none specified)")
                .replace("{{CONSTRAINTS}}", constraints_text or "(none specified)")
                .replace("{{CONTEXT}}", program.get("context", ""))
                .replace(
                    "{{CHECKPOINT_POLICY}}",
                    program.get("checkpoint_policy", "on-demand") or "on-demand",
                )
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
            step_backend = _resolve_entry_backend(entry, backend)
            model = resolve_model_preference(
                entry.get("model"), step_backend, role="continuous",
            )
            work_dir = state.get("work_dir", "")

            invocation = step_backend.build_spawn_invocation(
                prompt=prompt,
                model=model,
                work_dir=work_dir,
            )
            cmd = invocation.argv
            stdin_payload = step_backend.spawn_stdin_payload(prompt)

            (DATA_DIR / name).mkdir(parents=True, exist_ok=True)
            output_log = DATA_DIR / name / "output.log"

            try:
                proc = await _spawn_ai_subprocess(
                    cmd=cmd,
                    stdin_payload=stdin_payload,
                    output_log=output_log,
                    work_dir=work_dir,
                    env_overrides=invocation.env_overrides,
                    owner="continuous:%s" % name,
                )

                # ``create_subprocess_exec`` yields to the event loop.  A stop,
                # complete, or delete can therefore cancel the queue and write
                # a terminal state while the child is being created.  Re-read
                # both authorities before committing state=running; otherwise
                # the stale in-memory ``state`` would resurrect the task and
                # leave the just-spawned process untracked.
                fresh_state = load_state(Path(sf))
                queue_still_pending = any(
                    queued.get("type") == "continuous"
                    and queued.get("name") == name
                    and queued.get("status") == "pending"
                    for queued in load_queue()
                )
                if (
                    fresh_state is None
                    or not queue_still_pending
                    or not is_ready_for_next_step(fresh_state)
                ):
                    await _terminate_uncommitted_subprocess(proc)
                    current_status = (
                        fresh_state.get("status") if fresh_state is not None
                        else "missing"
                    )
                    append_log(
                        "%s -- DISPATCH ABORTED -- lifecycle state=%s"
                        % (name, current_status)
                    )
                    _journal_scheduler_event(
                        task_name=name,
                        event_type="dispatch_aborted",
                        outcome="lifecycle_race",
                        payload={"pid": proc.pid, "status": current_status},
                    )
                    continue

                state = fresh_state

                # Persist state=running BEFORE writing the lock file. If we
                # crash between the two writes, the next scheduler cycle sees
                # state="running" and triggers the orphan-recovery branch above
                # (check_lock → mark_step_failed). The reverse order would leak
                # a stale lock with a dead PID and leave state in a pre-running
                # status, allowing a silent re-dispatch that overwrites
                # output.log and stomps on the prior attempt.
                previous_orphan_detections = int(
                    state.get("orphan_detect_count") or 0
                )
                mark_step_started(state, step_number, step_description)
                save_state(Path(sf), state)

                _write_lock_file(lock_file, proc.pid)

                # Spec 006 T059: a start is successful only after both the
                # running state and its process lock are durable. Clear the
                # prior orphan episode at that point, then journal the single
                # recovery edge. Keeping the count through spawn/lock failure
                # prevents a failed retry from masquerading as recovery.
                if previous_orphan_detections > 0:
                    state["orphan_detect_count"] = 0
                    state["orphan_last_detected_ts"] = None
                    save_state(Path(sf), state)
                    _journal_scheduler_event(
                        task_name=name,
                        event_type="orphan_recovery",
                        outcome="cleared",
                        payload={
                            "previous_detected_cycles": previous_orphan_detections,
                            "step": step_number,
                            "pid": proc.pid,
                        },
                    )
                _journal_scheduler_event(
                    task_name=name,
                    event_type="step_start",
                    outcome="ok",
                    payload={"step": step_number, "pid": proc.pid},
                )

                # Start delivery watcher for output relay
                start_task_delivery_watch(entry, proc, output_log, lock_file, platform, step_backend, log)

                dispatched.append((name, proc.pid))
                append_log("%s -- DISPATCHED -- step %d PID %d" % (name, step_number, proc.pid))
                log.info(
                    "Continuous '%s': dispatched step %d (PID %d, model: %s)",
                    name, step_number, proc.pid, model,
                )
                # Spec 006 — journal the dispatch for [GET_EVENTS] queries.
                _journal_scheduler_event(
                    task_name=name,
                    event_type="dispatched",
                    outcome="ok",
                    payload={"step": step_number, "pid": proc.pid, "model": model},
                )

            except (OSError, ValueError) as exc:
                errors.append(name)
                append_log("%s -- ERROR -- step %d failed to spawn: %s" % (name, step_number, exc))
                log.error("Continuous '%s': failed to spawn step %d: %s", name, step_number, exc, exc_info=True)
                _journal_scheduler_event(
                    task_name=name,
                    event_type="error",
                    outcome="spawn_failed",
                    payload={"step": step_number, "exc": str(exc)},
                )

        except PersistenceUnavailableError as exc:
            errors.append(name or "?")
            append_log(
                "%s -- ERROR -- state unavailable; dispatch failed closed"
                % (name or "?")
            )
            log.critical(
                "Continuous '%s': state unavailable after recovery attempt; "
                "dispatch failed closed (%s)",
                name or "?",
                exc,
            )
            _journal_scheduler_event(
                task_name=name or "?",
                event_type="state_unavailable",
                outcome="failed_closed",
                payload={"operation": "continuous_dispatch"},
            )
        except Exception as exc:  # noqa: BLE001 - per-entry isolation boundary
            errors.append(name or "?")
            append_log(
                "%s -- ERROR -- dispatch crashed: %s" % (name or "?", exc)
            )
            log.error(
                "Continuous '%s': dispatch crashed (isolated from other "
                "tasks): %s", name or "?", exc, exc_info=True,
            )

    return dispatched, errors


# ── Log helper ───────────────────────────────────────────────────────────────


_append_log_lock = threading.Lock()


def append_log(entry: str) -> None:
    """Append a timestamped entry to bot.log (thread-safe)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with _append_log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (now, entry))


async def _dispatch_awaiting_reminders(platform, default_chat_id) -> int:
    """Spec 006 FR-011 — for each continuous task that has been
    awaiting input for longer than ``AWAITING_REMINDER_SECONDS`` and has
    not yet had a reminder for this episode, post exactly one reminder
    into its dedicated topic.

    Returns the number of reminders posted this cycle.
    """
    if platform is None:
        return 0

    try:
        from config import AWAITING_REMINDER_SECONDS, CONTINUOUS_DIR
    except Exception:
        return 0

    from continuous import load_state, save_state, state_file_path

    continuous_dir = Path(CONTINUOUS_DIR)
    if not continuous_dir.exists():
        return 0

    now = datetime.now(timezone.utc)
    threshold = AWAITING_REMINDER_SECONDS
    posted = 0

    for task_dir in sorted(continuous_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        sf = task_dir / "state.json"
        if not sf.exists():
            continue
        try:
            state = load_state(sf)
        except PersistenceUnavailableError as exc:
            log.critical(
                "Awaiting reminder skipped for '%s': continuous state is "
                "unavailable after recovery attempt (%s)",
                task_dir.name,
                exc,
            )
            _journal_scheduler_event(
                task_name=task_dir.name,
                event_type="state_unavailable",
                outcome="failed_closed",
                payload={"operation": "awaiting_reminder"},
            )
            continue
        if state is None:
            continue
        if state.get("status") not in ("awaiting_input", "awaiting-input"):
            continue
        if state.get("awaiting_reminder_sent_ts"):
            continue
        since_iso = state.get("awaiting_since_ts")
        if not since_iso:
            continue
        try:
            since_dt = datetime.fromisoformat(since_iso)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - since_dt).total_seconds() < threshold:
            continue

        dedicated = state.get("dedicated_thread_id")

        question = state.get("awaiting_question") or "a user decision"
        body = (
            "⏸ Still awaiting your reply on *%s*:\n\n%s\n\n"
            "Reply in this topic to resume the task."
            % (state.get("name") or task_dir.name, question)
        )
        delivered = False
        try:
            if dedicated is None:
                reason = "dedicated topic is missing"
            else:
                delivered = bool(
                    await platform.send_to_channel(dedicated, body, parse_mode="markdown")
                )
        except Exception as exc:
            from messaging.base import TopicUnreachable
            if not isinstance(exc, TopicUnreachable):
                log.warning(
                    "awaiting-reminder delivery failed for '%s': %s",
                    state.get("name"), exc,
                )
                continue
            reason = exc.reason or str(exc)
        else:
            if dedicated is not None:
                reason = "awaiting reminder delivery returned false"

        if not delivered:
            from topic_recovery import recover_unreachable_topic
            recovery = await recover_unreachable_topic(
                state.get("name") or task_dir.name,
                platform,
                reason=reason,
                event="awaiting_input",
                pending_delivery=body,
                hq_chat_id=default_chat_id,
            )
            delivered = recovery.pending_delivered
        if not delivered:
            continue

        state["awaiting_reminder_sent_ts"] = now.isoformat()
        try:
            save_state(state_file_path(state.get("name") or task_dir.name), state)
        except Exception:
            pass

        _journal_scheduler_event(
            task_name=state.get("name") or task_dir.name,
            event_type="awaiting_reminder_sent",
            outcome="posted",
            payload={"dedicated_thread_id": dedicated},
        )
        posted += 1

    if posted:
        log.info("Awaiting-input reminders posted this cycle: %d", posted)
    return posted


def _handle_continuous_orphan(state: dict, name: str) -> dict | None:
    """Spec 006 FR-022 orphan backoff.

    Counts consecutive orphan detections; once the threshold is reached
    escalates to a single incident (state=error + journal
    ``orphan_incident`` event with diagnostic payload). Below threshold:
    journals ``orphan_detected`` silently and marks the step failed so
    the next cycle can re-dispatch.

    Consecutiveness: detections older than 2× SCHEDULER_INTERVAL reset
    the counter to 1 (treat as a fresh episode).
    """
    try:
        from config import ORPHAN_INCIDENT_THRESHOLD as _threshold
        from config import SCHEDULER_INTERVAL as _cycle
    except Exception:
        _threshold, _cycle = 3, 60

    now = datetime.now(timezone.utc)
    last_iso = state.get("orphan_last_detected_ts")
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() > 2 * _cycle:
                state["orphan_detect_count"] = 0
        except ValueError:
            state["orphan_detect_count"] = 0

    state["orphan_detect_count"] = int(state.get("orphan_detect_count", 0)) + 1
    state["orphan_last_detected_ts"] = now.isoformat()

    count = state["orphan_detect_count"]

    if count < _threshold:
        # Below threshold — silent journal + mark step failed so
        # dispatch can retry next cycle.
        log.warning(
            "Continuous task '%s': orphan detected (cycle %d/%d) — "
            "marking step failed for re-dispatch",
            name, count, _threshold,
        )
        from continuous import mark_step_failed as _mark_step_failed
        _mark_step_failed(state, "subprocess exited unexpectedly")
        # mark_step_failed sets status=error, but below-threshold we want
        # to allow a retry — roll it back to 'pending' for the next tick.
        state["status"] = "pending"
        _journal_scheduler_event(
            task_name=name,
            event_type="orphan_detected",
            outcome="below_threshold",
            payload={"cycle": count, "threshold": _threshold},
        )
        return None

    # Threshold reached — escalate to single incident.
    from pathlib import Path as _Path
    from config import DATA_DIR as _DATA_DIR
    output_tail = ""
    last_exit_code = None
    try:
        log_path = _Path(_DATA_DIR) / name / "output.log"
        if log_path.exists():
            with log_path.open("rb") as fh:
                try:
                    fh.seek(-500, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                output_tail = fh.read().decode("utf-8", errors="replace")
    except Exception:
        output_tail = "(output.log not readable)"

    payload = {
        "detected_cycles": count,
        "last_output_tail": output_tail[-500:],
        "last_exit_code": last_exit_code,
        "lock_last_heartbeat_ts": None,
        "dedicated_thread_id": state.get("dedicated_thread_id"),
    }

    from continuous import mark_step_failed as _mark_step_failed
    _mark_step_failed(state, "orphan incident — backoff threshold reached")
    state["status"] = "error"

    _journal_scheduler_event(
        task_name=name,
        event_type="orphan_incident",
        outcome="escalated",
        payload=payload,
    )
    log.error(
        "Continuous task '%s': orphan incident after %d consecutive "
        "detections — state=error",
        name, count,
    )
    return payload


async def _deliver_orphan_incident(
    state: dict,
    name: str,
    payload: dict,
    platform,
) -> bool:
    """Post the one threshold incident, with recovery/HQ fallback support."""
    if platform is None:
        return False
    from scheduled_delivery import _build_continuous_header

    header, _ = _build_continuous_header(name, state, state_override="error")
    tail = (payload.get("last_output_tail") or "").strip()
    body = (
        "%s\n\nThe task process died repeatedly (%d detections) and has been "
        "stopped. Review the latest output and resume the task when ready."
        % (header, payload.get("detected_cycles") or 0)
    )
    if tail:
        body += "\n\nLast output:\n```\n%s\n```" % tail[-500:]
    dedicated = state.get("dedicated_thread_id")
    reason = "dedicated topic is missing"
    delivered = False
    if dedicated is not None:
        try:
            delivered = bool(
                await platform.send_to_channel(dedicated, body, parse_mode="Markdown")
            )
        except Exception as exc:
            from messaging.base import TopicUnreachable
            if not isinstance(exc, TopicUnreachable):
                log.error("Orphan incident delivery failed for %s: %s", name, exc)
                return False
            reason = exc.reason or str(exc)
        else:
            reason = "orphan incident delivery returned false"
    if delivered:
        return True
    from topic_recovery import recover_unreachable_topic
    recovery = await recover_unreachable_topic(
        name,
        platform,
        reason=reason,
        event="task_death",
        pending_delivery=body,
    )
    return recovery.pending_delivered


def _journal_scheduler_event(
    task_name: str,
    event_type: str,
    outcome: str,
    payload: dict | None = None,
    task_type: str = "continuous",
) -> None:
    """Append a scheduler-origin event to the spec-006 event journal.

    Safe wrapper: silently swallows any journal error so a logging issue
    never takes down a dispatch path. The journal itself logs at WARN/
    ERROR level if writes fail.
    """
    try:
        import events as events_mod
        events_mod.append(
            task_name=task_name,
            task_type=task_type,
            event_type=event_type,
            outcome=outcome,
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.error(
            "journal event append failed for %s (%s): %s",
            task_name, event_type, exc,
        )


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
    """Run one cycle under a shared maintenance lease.

    Update writer intent rejects a new tick before it can claim queue entries,
    so the exclusive updater never snapshots state concurrently with a cycle.
    """
    try:
        async with get_maintenance_gate().shared():
            return await _run_scheduler_cycle(
                backend,
                platform=platform,
                default_chat_id=default_chat_id,
            )
    except MaintenanceActiveError:
        return {
            "dispatched": [],
            "errors": [],
            "reminders_sent": 0,
            "maintenance": True,
        }


async def _run_scheduler_cycle(
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

    dispatched: list[tuple[str, int]] = []
    errors: list[str] = []
    reminders_sent = 0
    queue_failed = False

    try:
        due_tasks, due_reminders = _claim_due_entries()
    except QueueUnavailableError as exc:
        log.critical(
            "Scheduler cycle stopped: queue unavailable after recovery "
            "attempt (%s). No task mutation or dispatch was performed.",
            exc,
        )
        _journal_scheduler_event(
            task_name="scheduler-queue",
            task_type="scheduler",
            event_type="queue_unavailable",
            outcome="failed_closed",
            payload={"operation": "claim"},
        )
        return {
            "dispatched": dispatched,
            "errors": ["queue_unavailable"],
            "reminders_sent": reminders_sent,
            "degraded": True,
        }

    # Dispatch reminders (no LLM)
    if due_reminders:
        try:
            await _dispatch_reminders(
                due_reminders,
                platform,
                default_chat_id=default_chat_id,
            )
        except QueueUnavailableError as exc:
            log.critical("Reminder reconciliation failed closed: %s", exc)
            errors.append("queue_unavailable")
            queue_failed = True
            _journal_scheduler_event(
                task_name="scheduler-queue",
                task_type="scheduler",
                event_type="queue_unavailable",
                outcome="failed_closed",
                payload={"operation": "reminder_reconcile"},
            )
        reminders_sent = len(due_reminders)

    # Dispatch agent tasks (one-shot / periodic)
    if due_tasks and not queue_failed:
        try:
            task_dispatched, task_errors = await _dispatch_agent_tasks(
                due_tasks,
                backend,
                platform,
            )
            dispatched.extend(task_dispatched)
            errors.extend(task_errors)
        except QueueUnavailableError as exc:
            log.critical("Task reconciliation failed closed: %s", exc)
            errors.append("queue_unavailable")
            queue_failed = True
            _journal_scheduler_event(
                task_name="scheduler-queue",
                task_type="scheduler",
                event_type="queue_unavailable",
                outcome="failed_closed",
                payload={"operation": "task_reconcile"},
            )

    # Dispatch continuous task steps (checked independently of the claim system)
    if not queue_failed:
        try:
            cont_dispatched, cont_errors = await _handle_continuous_entries(backend, platform)
            dispatched.extend(cont_dispatched)
            errors.extend(cont_errors)
        except QueueUnavailableError as exc:
            log.critical("Continuous scheduling skipped: queue unavailable: %s", exc)
            errors.append("queue_unavailable")
            queue_failed = True
        except Exception as exc:
            log.error("Continuous task handling failed: %s", exc, exc_info=True)

    # Retry persisted dedicated-topic failures independently of task dispatch.
    # This is what advances the retry window when no new step/reminder occurs.
    try:
        from topic_recovery import retry_unreachable_topics
        await retry_unreachable_topics(
            platform,
            hq_chat_id=default_chat_id,
        )
    except Exception as exc:
        log.error("Dedicated-topic recovery failed: %s", exc, exc_info=True)

    # Spec 006 — rotate the hot event journal (hourly + size-based) and
    # prune retention once per cycle. Idempotent and cheap.
    try:
        import events as events_mod
        events_mod.rotate_if_needed()
    except Exception as exc:
        log.error("events.rotate_if_needed failed: %s", exc, exc_info=True)

    # Spec 006 FR-011 — 24h awaiting-input reminder per dedicated topic.
    try:
        await _dispatch_awaiting_reminders(platform, default_chat_id)
    except Exception as exc:
        log.error("awaiting-reminder loop failed: %s", exc, exc_info=True)

    result = {
        "dispatched": dispatched,
        "errors": errors,
        "reminders_sent": reminders_sent,
    }
    if queue_failed:
        result["degraded"] = True
    return result


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
    # A missing live file with a recovery marker is *not* a fresh install: it
    # means a prior process stopped between quarantine and restore. Resume (or
    # fail) that recovery before considering legacy migration, otherwise the
    # migration could overwrite evidence with a newly-built queue.
    marker = recovery_marker_path(QUEUE_FILE)
    if QUEUE_FILE.exists() or marker.exists():
        with _queue_mutex():
            _load_queue_unlocked()
        log.info("queue.json already exists or was recovered — skipping migration")
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
                        # Validate scheduled_at for one-shot entries before
                        # the scheduler ingests them — a corrupt legacy
                        # entry otherwise stays wedged in queue.json.
                        if migrated_entry.get("type") == "one-shot":
                            try:
                                migrated_entry["scheduled_at"] = (
                                    validate_one_shot_scheduled_at(
                                        migrated_entry.get("scheduled_at"),
                                        label="migrated one-shot",
                                    )
                                )
                            except ValueError as v_exc:
                                log.warning(
                                    "Skipping corrupt timed entry %s: %s",
                                    entry.get("name"), v_exc,
                                )
                                continue
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
