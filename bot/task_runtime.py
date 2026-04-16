"""Helpers for resolving the runtime context of scheduled tasks."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import config as _config
from agents import Agent

log = logging.getLogger("robyx.task_runtime")


@dataclass(frozen=True)
class TaskRuntimeContext:
    agent_name: str
    agent_type: str
    work_dir: str


def validate_task_name(task_name: str) -> str:
    """Return a safe task name or raise ``ValueError``.

    Task names are used as directory names under ``data/`` for lock files
    and logs. They must therefore stay confined to a single path segment.
    """
    value = str(task_name or "").strip()
    if not value:
        raise ValueError("task name is required")
    if any(ch in value for ch in ("\n", "\r", "\t", "\0", "|")):
        raise ValueError("task name contains unsupported characters")

    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or len(path.parts) != 1 or path.name in {".", ".."}:
        raise ValueError("task name must be a single relative path segment")

    return value


def validate_agent_file_ref(agent_file: str) -> tuple[str, str]:
    """Return ``(normalized_ref, agent_type)`` for a safe agent-file ref.

    Scheduled tasks may only reference workspace or specialist brief files
    under ``agents/<name>.md`` or ``specialists/<name>.md``.
    """
    value = str(agent_file or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("agent_file is required")
    if any(ch in value for ch in ("\n", "\r", "\t", "\0", "|")):
        raise ValueError("agent_file contains unsupported characters")

    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 2:
        raise ValueError(
            "agent_file must be 'agents/<name>.md' or 'specialists/<name>.md'"
        )

    parent, filename = path.parts
    if parent not in {"agents", "specialists"}:
        raise ValueError(
            "agent_file must be 'agents/<name>.md' or 'specialists/<name>.md'"
        )
    if path.suffix != ".md" or path.stem in {"", ".", ".."}:
        raise ValueError("agent_file must point to a markdown brief file")

    return value, ("specialist" if parent == "specialists" else "workspace")


def resolve_agent_file_path(data_dir: Path, agent_file: str) -> tuple[str, str, Path]:
    """Resolve a validated agent-file ref against ``data/``."""
    normalized, agent_type = validate_agent_file_ref(agent_file)
    return normalized, agent_type, data_dir / normalized


def _infer_agent_ref(task: dict) -> tuple[str, str]:
    """Infer ``(agent_name, agent_type)`` from a scheduled task payload."""
    fallback_name = str(task.get("name") or "").strip()
    try:
        agent_file, agent_type = validate_agent_file_ref(task.get("agent_file") or "")
    except ValueError:
        return fallback_name, "workspace"
    path = PurePosixPath(agent_file)
    return path.stem or fallback_name, agent_type


def _load_agent_snapshot(agent_name: str) -> Agent | None:
    """Load an agent from ``state.json`` without mutating live state."""
    if not agent_name or not _config.STATE_FILE.exists():
        return None

    try:
        payload = json.loads(_config.STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read state file for scheduled task context: %s", exc)
        return None

    agent_data = payload.get("agents", {}).get(agent_name)
    if not isinstance(agent_data, dict):
        return None

    try:
        return Agent.from_dict(agent_data)
    except TypeError as exc:
        log.warning(
            "Invalid agent record in state file for '%s': %s",
            agent_name,
            exc,
        )
        return None


def resolve_task_runtime_context(task: dict) -> TaskRuntimeContext:
    """Resolve the target agent identity and working directory for a task."""
    agent_name, agent_type = _infer_agent_ref(task)
    agent = _load_agent_snapshot(agent_name)
    if agent:
        return TaskRuntimeContext(
            agent_name=agent.name,
            agent_type=agent.agent_type,
            work_dir=agent.work_dir,
        )

    fallback_work_dir = str(_config.WORKSPACE)
    if agent_name:
        # Promoted from INFO to WARNING: if an agent referenced by a
        # scheduled task no longer exists in state.json, the task runs
        # against ROBYX_WORKSPACE instead of the agent's original cwd.
        # That usually indicates a deleted agent with lingering queue
        # entries — operator should see it without grepping the log.
        log.warning(
            "No stored agent runtime context for scheduled task '%s'; "
            "falling back to ROBYX_WORKSPACE=%s (agent likely deleted — "
            "consider cancelling its queue entries)",
            agent_name,
            fallback_work_dir,
        )

    return TaskRuntimeContext(
        agent_name=agent_name,
        agent_type=agent_type,
        work_dir=fallback_work_dir,
    )
