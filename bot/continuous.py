"""Robyx — Continuous Task State Management.

Manages the lifecycle of iterative autonomous tasks. Each continuous task
has a state file at ``data/continuous/<name>/state.json`` that tracks the
program objective, step history, and next planned step.

The scheduler reads this state to decide whether to dispatch the next step.
The step agent reads it for context and updates it with results.
"""

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import CONTINUOUS_DIR

log = logging.getLogger("robyx.continuous")


# ── State I/O ────────────────────────────────────────────────────────────────


def _state_dir(name: str) -> Path:
    return CONTINUOUS_DIR / name


def state_file_path(name: str) -> Path:
    return _state_dir(name) / "state.json"


def plan_file_path(name: str) -> Path:
    """Path to the task-specific plan markdown (spec 005).

    The plan is produced at continuous-task creation time from the
    ``CONTINUOUS_PROGRAM`` payload and is read by (a) the primary agent on
    demand via ``[GET_PLAN]`` and (b) the secondary step agent's prompt
    template as shared knowledge.
    """
    return _state_dir(name) / "plan.md"


def write_plan_md(name: str, content: str) -> Path:
    """Atomically persist the continuous task's plan.md (spec 005).

    Uses the same write-then-rename + fsync pattern as ``save_state``.
    """
    path = plan_file_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content or "")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read_plan_md(name: str) -> str | None:
    """Read the continuous task's plan.md, or None if not present."""
    path = plan_file_path(name)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Failed to read plan.md for '%s': %s", name, exc)
        return None


_SPEC_006_DEFAULTS: dict[str, Any] = {
    "dedicated_thread_id": None,
    "drain_timeout_seconds": 3600,
    "awaiting_since_ts": None,
    "awaiting_pinned_msg_id": None,
    "awaiting_reminder_sent_ts": None,
    "orphan_detect_count": 0,
    "orphan_last_detected_ts": None,
    "hq_fallback_sent": False,
    "topic_unreachable_since_ts": None,
    "archived_at": None,
    "migrated_v0_26_0": None,
}


def load_state(path: Path) -> dict | None:
    """Load a continuous task state file. Returns None if missing or corrupt.

    Spec 006: applies legacy status normalisation (``awaiting-input`` →
    ``awaiting_input``, ``rate-limited`` → ``rate_limited``, ``paused`` →
    ``stopped``) in-memory so older snapshots keep working. Defaults any
    missing spec-006 fields so downstream code can rely on their presence.
    The caller decides whether to persist the normalised form (save_state
    automatically writes canonical form).
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load continuous state %s: %s", path, exc)
        return None

    if not isinstance(data, dict):
        return None

    # Spec 006 — normalise legacy status strings and default new fields.
    try:
        from continuous_state_machine import normalize_legacy_status
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            data["status"] = normalize_legacy_status(raw_status)
    except ImportError:
        # Circular import protection — tests that import continuous before
        # the full module tree is loaded. Status normalisation is best-effort.
        pass

    for field, default in _SPEC_006_DEFAULTS.items():
        data.setdefault(field, default)

    return data


def save_state(path: Path, state: dict) -> None:
    """Atomically persist a continuous task state (write-then-rename, fsynced)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── State creation ───────────────────────────────────────────────────────────


def create_continuous_task(
    name: str,
    parent_workspace: str,
    program: dict,
    thread_id: Any,
    branch: str,
    work_dir: str,
) -> dict:
    """Create a new continuous task state and persist it.

    Parameters
    ----------
    name : str
        Unique slug for the task (also used as directory name).
    parent_workspace : str
        Name of the parent workspace agent that spawned this task.
    program : dict
        Must contain: ``objective``, ``success_criteria`` (list),
        ``constraints`` (list), ``checkpoint_policy`` (str),
        ``context`` (str, additional context for each step).
    thread_id : Any
        Platform thread/channel ID for the continuous task's topic.
    branch : str
        Git branch name (e.g., ``continuous/improve-deconvolution``).
    work_dir : str
        Filesystem path where the agent operates.
    """
    now = datetime.now(timezone.utc).isoformat()
    state: dict[str, Any] = {
        "id": str(_uuid.uuid4()),
        "name": name,
        "status": "pending",  # canonical spec-006 states: pending | running | awaiting_input | rate_limited | stopped | completed | error | deleted
        "parent_workspace": parent_workspace,
        "workspace_thread_id": thread_id,
        "branch": branch,
        "work_dir": work_dir,
        "created_at": now,
        "updated_at": now,
        "program": {
            "objective": program.get("objective", ""),
            "success_criteria": program.get("success_criteria", []),
            "constraints": program.get("constraints", []),
            "checkpoint_policy": program.get("checkpoint_policy", "on-demand"),
            "context": program.get("context", ""),
        },
        "current_step": None,
        "next_step": program.get("first_step", {
            "number": 1,
            "description": "Begin work based on the program objective.",
        }),
        "history": [],
        "total_steps_completed": 0,
        "rate_limited_until": None,
    }
    # Spec 006 additive fields — present from creation so every code path
    # can rely on them without defensive .get() calls.
    for field, default in _SPEC_006_DEFAULTS.items():
        state[field] = default
    # Thread routing: at create time, both workspace_thread_id (legacy
    # parent-chat delivery target) and dedicated_thread_id (post-spec-006
    # dedicated-topic target) can be present. The caller in topics.py
    # fills dedicated_thread_id after create_channel returns.

    path = state_file_path(name)
    save_state(path, state)
    log.info("Created continuous task '%s' at %s", name, path)
    return state


# ── Step lifecycle ───────────────────────────────────────────────────────────


def mark_step_started(state: dict, step_number: int, description: str) -> dict:
    """Mark that a step has started execution."""
    state["status"] = "running"
    state["current_step"] = {
        "number": step_number,
        "description": description,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def mark_step_completed(state: dict, artifact: str, duration_seconds: int) -> dict:
    """Mark the current step as completed and record it in history."""
    now = datetime.now(timezone.utc).isoformat()
    step = state.get("current_step")
    if step:
        step["status"] = "completed"
        step["completed_at"] = now
        state["history"].append({
            "step": step["number"],
            "description": step["description"],
            "artifact": artifact,
            "duration_seconds": duration_seconds,
            "completed_at": now,
        })
    state["total_steps_completed"] = len(state["history"])
    state["current_step"] = None
    state["updated_at"] = now
    return state


def mark_step_failed(state: dict, error: str) -> dict:
    """Mark the current step as failed."""
    now = datetime.now(timezone.utc).isoformat()
    step = state.get("current_step")
    if step:
        step["status"] = "failed"
        step["error"] = error
        step["failed_at"] = now
    state["status"] = "error"
    state["updated_at"] = now
    return state


def set_next_step(state: dict, description: str) -> dict:
    """Plan the next step to be executed."""
    next_number = state.get("total_steps_completed", 0) + 1
    state["next_step"] = {
        "number": next_number,
        "description": description,
    }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


# ── Status transitions ───────────────────────────────────────────────────────


def pause_task(state: dict) -> dict:
    """Stop a continuous task. The scheduler will not dispatch new steps.

    Spec 006: renamed ``paused`` → ``stopped`` for consistency with the
    lifecycle contract. The word "pause" is retained in this function
    name for source-level continuity but the status written is now the
    canonical ``stopped``. Legacy ``paused`` values on disk are
    normalised by ``load_state`` on read.
    """
    state["status"] = "stopped"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def resume_task(state: dict) -> dict:
    """Resume a stopped, rate-limited, awaiting-input, or errored task.

    Clears awaiting-related fields so the next scheduler tick sees a
    clean ``pending`` state. The scheduler will transition
    ``pending → running`` on its next dispatch.
    """
    state["status"] = "pending"
    state["rate_limited_until"] = None
    state.pop("awaiting_question", None)
    state["awaiting_since_ts"] = None
    state["awaiting_pinned_msg_id"] = None
    state["awaiting_reminder_sent_ts"] = None
    # Resume on an errored task resets the orphan counter — it's a fresh
    # attempt per the spec-006 recovery contract.
    state["orphan_detect_count"] = 0
    state["orphan_last_detected_ts"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def complete_task(state: dict) -> dict:
    """Mark a continuous task as completed (objective reached)."""
    state["status"] = "completed"
    state["current_step"] = None
    state["next_step"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def set_awaiting_input(state: dict, question: str = "") -> dict:
    """Mark a task as waiting for user input (spec 006 canonical state).

    Also sets ``awaiting_since_ts`` used by the scheduler's 24-hour
    reminder loop, and clears any prior ``awaiting_reminder_sent_ts``
    so the new awaiting episode starts a fresh reminder cycle.
    """
    now = datetime.now(timezone.utc)
    state["status"] = "awaiting_input"
    if question:
        state["awaiting_question"] = question
    state["awaiting_since_ts"] = now.isoformat()
    state["awaiting_reminder_sent_ts"] = None
    state["awaiting_pinned_msg_id"] = None  # pin set separately by delivery layer
    state["updated_at"] = now.isoformat()
    return state


def set_rate_limited(state: dict, retry_after_seconds: int = 3600) -> dict:
    """Mark a task as rate-limited (spec 006 canonical underscore form)."""
    now = datetime.now(timezone.utc)
    retry_at = datetime.fromtimestamp(
        now.timestamp() + retry_after_seconds, tz=timezone.utc,
    )
    state["status"] = "rate_limited"
    state["rate_limited_until"] = retry_at.isoformat()
    state["updated_at"] = now.isoformat()
    return state


async def update_topic_state_marker(state: dict, platform, display_name: str | None = None) -> bool:
    """Spec 006 FR-009 — refresh the dedicated topic's title suffix from
    the task's current ``status``.

    No-op if the task has no ``dedicated_thread_id`` or the platform
    lacks ``edit_topic_title``. Catches ``TopicUnreachable`` and
    propagates it so the scheduler-side recovery path (FR-002a) can
    engage. All other exceptions are logged and swallowed — a failed
    title update must not block the state transition.
    """
    dedicated = state.get("dedicated_thread_id")
    if dedicated is None or platform is None:
        return False
    if not hasattr(platform, "edit_topic_title"):
        return False

    from continuous_state_machine import marker_suffix
    suffix = marker_suffix(state.get("status") or "running")
    name = display_name or state.get("name") or "?"
    new_title = "[Continuous] %s%s" % (name, suffix)

    try:
        return bool(await platform.edit_topic_title(dedicated, new_title))
    except Exception as exc:
        # TopicUnreachable is significant; re-raise so the scheduler's
        # recovery layer can see it. Everything else: log + swallow.
        from messaging.base import TopicUnreachable
        if isinstance(exc, TopicUnreachable):
            raise
        log.warning(
            "update_topic_state_marker failed for '%s' (thread=%s): %s",
            name, dedicated, exc,
        )
        return False


async def pin_awaiting_message(
    state: dict,
    platform,
    chat_id,
    message_id: int,
) -> bool:
    """Spec 006 FR-010 — pin an awaiting-input message in the task's
    dedicated topic and record ``awaiting_pinned_msg_id`` in the state.
    """
    dedicated = state.get("dedicated_thread_id")
    if dedicated is None or platform is None:
        return False
    if not hasattr(platform, "pin_message"):
        return False

    try:
        ok = await platform.pin_message(
            chat_id=chat_id,
            thread_id=dedicated,
            message_id=message_id,
        )
    except Exception as exc:
        from messaging.base import TopicUnreachable
        if isinstance(exc, TopicUnreachable):
            raise
        log.warning(
            "pin_awaiting_message failed for task '%s': %s",
            state.get("name"), exc,
        )
        return False
    if ok:
        state["awaiting_pinned_msg_id"] = message_id
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return bool(ok)


async def unpin_awaiting_message(
    state: dict,
    platform,
    chat_id,
) -> bool:
    """Spec 006 FR-012 — unpin the currently-pinned awaiting message
    (if any) in the task's dedicated topic.
    """
    dedicated = state.get("dedicated_thread_id")
    pinned = state.get("awaiting_pinned_msg_id")
    if dedicated is None or platform is None or pinned is None:
        return False
    if not hasattr(platform, "unpin_message"):
        return False

    try:
        ok = await platform.unpin_message(
            chat_id=chat_id,
            thread_id=dedicated,
            message_id=pinned,
        )
    except Exception as exc:
        from messaging.base import TopicUnreachable
        if isinstance(exc, TopicUnreachable):
            raise
        log.warning(
            "unpin_awaiting_message failed for task '%s': %s",
            state.get("name"), exc,
        )
        return False
    if ok:
        state["awaiting_pinned_msg_id"] = None
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return bool(ok)


def check_rate_limit_recovery(state: dict) -> bool:
    """Return True if a rate-limited task can be retried now.

    Accepts both canonical ``rate_limited`` and legacy ``rate-limited``.
    """
    status = state.get("status")
    if status not in ("rate_limited", "rate-limited"):
        return False
    retry_until = state.get("rate_limited_until")
    if not retry_until:
        return True
    try:
        retry_at = datetime.fromisoformat(retry_until)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= retry_at
    except ValueError:
        return True


# ── Query helpers ────────────────────────────────────────────────────────────


def is_ready_for_next_step(state: dict) -> bool:
    """Return True if the task has a planned next step and is not blocked.

    Accepts both canonical spec-006 status names and their legacy aliases.
    Blocked states: running, awaiting_input, stopped, completed, error,
    deleted, rate_limited (unless recovery time has passed).
    """
    status = state.get("status")
    if status in (
        "completed", "deleted",
        "stopped", "paused",
        "awaiting_input", "awaiting-input",
        "running",
        "error",
    ):
        return False
    if status in ("rate_limited", "rate-limited"):
        return check_rate_limit_recovery(state)
    next_step = state.get("next_step")
    return next_step is not None and bool(next_step.get("description"))


def build_step_context(state: dict) -> str:
    """Build a summary of previous steps for the step agent's context.

    Robust against malformed history entries: a step agent that drifts
    from the documented schema (for instance writing ``summary`` instead
    of ``description``, or omitting ``step``) must NOT crash the
    scheduler's dispatch loop. We fall back across common alternate keys
    and skip entries that are truly unreadable, logging a warning so the
    drift is visible without being fatal.
    """
    lines = []
    for entry in state.get("history", [])[-10:]:  # Last 10 steps
        if not isinstance(entry, dict):
            log.warning(
                "build_step_context: skipping non-dict history entry: %r",
                entry,
            )
            continue
        step_num = entry.get("step")
        description = (
            entry.get("description")
            or entry.get("summary")
            or entry.get("artifact")
            or "(no description)"
        )
        artifact = entry.get("artifact", "n/a")
        try:
            desc_str = str(description)[:80]
            step_label = (
                "Step %d" % int(step_num) if step_num is not None else "Step ?"
            )
            lines.append("%s: %s → %s" % (step_label, desc_str, artifact))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "build_step_context: skipping unrenderable entry %r: %s",
                entry, exc,
            )
    return "\n".join(lines) if lines else "(no previous steps)"
