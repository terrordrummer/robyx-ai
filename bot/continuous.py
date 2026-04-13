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


def load_state(path: Path) -> dict | None:
    """Load a continuous task state file. Returns None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load continuous state %s: %s", path, exc)
        return None


def save_state(path: Path, state: dict) -> None:
    """Atomically persist a continuous task state (write-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
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
    state = {
        "id": str(_uuid.uuid4()),
        "name": name,
        "status": "pending",  # pending, running, paused, awaiting-input, completed, error, rate-limited
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
    """Pause a continuous task. The scheduler will not dispatch new steps."""
    state["status"] = "paused"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def resume_task(state: dict) -> dict:
    """Resume a paused or rate-limited task."""
    state["status"] = "pending"
    state["rate_limited_until"] = None
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
    """Mark a task as waiting for user input."""
    state["status"] = "awaiting-input"
    if question:
        state["awaiting_question"] = question
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def set_rate_limited(state: dict, retry_after_seconds: int = 3600) -> dict:
    """Mark a task as rate-limited. The scheduler will skip until recovery."""
    now = datetime.now(timezone.utc)
    retry_at = datetime.fromtimestamp(
        now.timestamp() + retry_after_seconds, tz=timezone.utc,
    )
    state["status"] = "rate-limited"
    state["rate_limited_until"] = retry_at.isoformat()
    state["updated_at"] = now.isoformat()
    return state


def check_rate_limit_recovery(state: dict) -> bool:
    """Return True if a rate-limited task can be retried now."""
    if state.get("status") != "rate-limited":
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
    """Return True if the task has a planned next step and is not blocked."""
    status = state.get("status")
    if status in ("completed", "paused", "awaiting-input", "running"):
        return False
    if status == "rate-limited":
        return check_rate_limit_recovery(state)
    next_step = state.get("next_step")
    return next_step is not None and bool(next_step.get("description"))


def build_step_context(state: dict) -> str:
    """Build a summary of previous steps for the step agent's context."""
    lines = []
    for entry in state.get("history", [])[-10:]:  # Last 10 steps
        lines.append(
            "Step %d: %s → %s"
            % (entry["step"], entry["description"][:80], entry.get("artifact", "n/a"))
        )
    return "\n".join(lines) if lines else "(no previous steps)"
