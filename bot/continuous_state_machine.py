"""Continuous-task lifecycle state machine (spec 006).

Formalises the states and transitions that were previously spread
across string checks in ``continuous.py``, ``scheduler.py``, and
``lifecycle_macros.py``. The authoritative state is stored as the
``status`` string inside each task's ``data/continuous/<name>/state.json``.

Canonical state names use underscore form (``awaiting_input``,
``rate_limited``). Legacy hyphen-form values (``awaiting-input``,
``rate-limited``) are tolerated on read and normalised on write so
pre-0.26.0 snapshots keep working without a forced data rewrite.

Contracts: ``specs/006-continuous-task-robustness/contracts/lifecycle-ops.md``,
``specs/006-continuous-task-robustness/data-model.md`` §1.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class ContinuousStatus(str, Enum):
    """Authoritative set of continuous-task states.

    ``PENDING`` is the "ready to be dispatched" state that a task
    occupies at creation and after a resume. The scheduler wakes tasks
    from this state. It is distinct from ``RUNNING`` (a step is actively
    being executed) even though the wall-clock delay between the two can
    be as short as one scheduler cycle (60 s).
    """
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    RATE_LIMITED = "rate_limited"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"
    DELETED = "deleted"

    @classmethod
    def all_values(cls) -> frozenset[str]:
        return frozenset(s.value for s in cls)


# Legacy aliases accepted on read. Mapping → canonical underscore value.
_LEGACY_ALIASES = {
    "awaiting-input": ContinuousStatus.AWAITING_INPUT.value,
    "rate-limited": ContinuousStatus.RATE_LIMITED.value,
    "paused": ContinuousStatus.STOPPED.value,  # pre-spec-006 name for stopped
    "created": ContinuousStatus.PENDING.value,  # early drafts of spec used "created"
}


class InvalidTransition(ValueError):
    """Raised when code attempts an undefined lifecycle transition."""

    def __init__(self, current: str, target: str, detail: str = "") -> None:
        msg = "Invalid transition: %s → %s" % (current, target)
        if detail:
            msg += " (%s)" % detail
        super().__init__(msg)
        self.current = current
        self.target = target
        self.detail = detail


# Transition table. Each source state maps to the set of valid targets.
# Terminal states (completed, deleted) have empty outgoing sets.
#
# ERROR is reachable from any state except terminal (orphan_incident
# escalation or explicit error transition from user code). DELETED is
# reachable from any non-deleted state.
_TRANSITIONS: dict[str, frozenset[str]] = {
    ContinuousStatus.PENDING.value: frozenset({
        ContinuousStatus.RUNNING.value,
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.ERROR.value,
        ContinuousStatus.DELETED.value,
    }),
    ContinuousStatus.RUNNING.value: frozenset({
        ContinuousStatus.PENDING.value,   # step ends, awaits next cycle
        ContinuousStatus.AWAITING_INPUT.value,
        ContinuousStatus.RATE_LIMITED.value,
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.COMPLETED.value,
        ContinuousStatus.ERROR.value,
        ContinuousStatus.DELETED.value,
    }),
    ContinuousStatus.AWAITING_INPUT.value: frozenset({
        ContinuousStatus.PENDING.value,   # resume → ready for next dispatch
        ContinuousStatus.RUNNING.value,
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.COMPLETED.value,
        ContinuousStatus.ERROR.value,
        ContinuousStatus.DELETED.value,
    }),
    ContinuousStatus.RATE_LIMITED.value: frozenset({
        ContinuousStatus.PENDING.value,   # recovery timestamp passed
        ContinuousStatus.RUNNING.value,
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.COMPLETED.value,
        ContinuousStatus.ERROR.value,
        ContinuousStatus.DELETED.value,
    }),
    ContinuousStatus.STOPPED.value: frozenset({
        ContinuousStatus.PENDING.value,   # resume → ready for next dispatch
        ContinuousStatus.RUNNING.value,
        ContinuousStatus.COMPLETED.value,
        ContinuousStatus.ERROR.value,
        ContinuousStatus.DELETED.value,
    }),
    # Terminal: only delete is allowed.
    ContinuousStatus.COMPLETED.value: frozenset({
        ContinuousStatus.DELETED.value,
    }),
    # Error is recoverable via resume (→ pending → running) or stop/delete.
    ContinuousStatus.ERROR.value: frozenset({
        ContinuousStatus.PENDING.value,
        ContinuousStatus.RUNNING.value,
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.DELETED.value,
    }),
    # Terminal, unrecoverable.
    ContinuousStatus.DELETED.value: frozenset(),
}


def normalize_legacy_status(status: str) -> str:
    """Map legacy / hyphenated state names to their canonical underscore form.

    Unknown status values are returned unchanged (caller decides whether
    to raise or accept).
    """
    if not isinstance(status, str):
        return str(status)
    status = status.strip()
    return _LEGACY_ALIASES.get(status, status)


def is_valid_status(status: str) -> bool:
    """True if ``status`` is a canonical state (post-normalisation)."""
    return normalize_legacy_status(status) in ContinuousStatus.all_values()


def is_terminal(status: str) -> bool:
    """True if ``status`` is a terminal state (completed or deleted)."""
    status = normalize_legacy_status(status)
    return status in (
        ContinuousStatus.COMPLETED.value,
        ContinuousStatus.DELETED.value,
    )


def is_resumable(status: str) -> bool:
    """True if ``status`` can transition back to running via resume_task."""
    status = normalize_legacy_status(status)
    return status in (
        ContinuousStatus.STOPPED.value,
        ContinuousStatus.AWAITING_INPUT.value,
        ContinuousStatus.RATE_LIMITED.value,
        ContinuousStatus.ERROR.value,
    )


def validate_transition(current: str, target: str) -> None:
    """Raise InvalidTransition if ``current → target`` is not allowed.

    Both inputs are normalised before lookup, so legacy hyphen-form names
    work transparently. Transitions to the same state (current == target)
    are always allowed (idempotent operations).
    """
    current_norm = normalize_legacy_status(current)
    target_norm = normalize_legacy_status(target)

    if current_norm == target_norm:
        return

    if target_norm not in ContinuousStatus.all_values():
        raise InvalidTransition(
            current, target, "target is not a valid status",
        )
    if current_norm not in _TRANSITIONS:
        raise InvalidTransition(
            current, target, "current is not a valid status",
        )
    allowed = _TRANSITIONS[current_norm]
    if target_norm not in allowed:
        raise InvalidTransition(current, target)


def valid_targets(current: str) -> frozenset[str]:
    """Return the set of valid target statuses from ``current``."""
    current_norm = normalize_legacy_status(current)
    return _TRANSITIONS.get(current_norm, frozenset())


# Title-suffix mapping used by topic-title state markers.
STATE_MARKER_SUFFIX: dict[str, str] = {
    ContinuousStatus.PENDING.value: " · ▶",
    ContinuousStatus.RUNNING.value: " · ▶",
    ContinuousStatus.AWAITING_INPUT.value: " · ⏸",
    ContinuousStatus.RATE_LIMITED.value: " · ⏳",
    ContinuousStatus.STOPPED.value: " · ⏹",
    ContinuousStatus.COMPLETED.value: " · ✅",
    ContinuousStatus.ERROR.value: " · ❌",
    ContinuousStatus.DELETED.value: "",
}


def marker_suffix(status: str) -> str:
    """Return the title suffix for ``status``. Empty for deleted state."""
    status_norm = normalize_legacy_status(status)
    return STATE_MARKER_SUFFIX.get(status_norm, "")


def canonical_values() -> Iterable[str]:
    """Iterator over every canonical status value — useful for tests."""
    return (s.value for s in ContinuousStatus)
