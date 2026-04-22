"""Tests for bot/continuous_state_machine.py (spec 006).

Contract: ``specs/006-continuous-task-robustness/contracts/lifecycle-ops.md``
state diagram.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sm():
    import continuous_state_machine as sm_mod  # type: ignore
    return sm_mod


# ── Normalisation ──────────────────────────────────────────────────────────


def test_normalize_legacy_awaiting_input(sm):
    assert sm.normalize_legacy_status("awaiting-input") == "awaiting_input"


def test_normalize_legacy_rate_limited(sm):
    assert sm.normalize_legacy_status("rate-limited") == "rate_limited"


def test_normalize_legacy_paused(sm):
    assert sm.normalize_legacy_status("paused") == "stopped"


def test_normalize_canonical_pass_through(sm):
    for value in ("pending", "running", "awaiting_input", "rate_limited",
                  "stopped", "completed", "error", "deleted"):
        assert sm.normalize_legacy_status(value) == value


def test_normalize_unknown_returns_as_is(sm):
    assert sm.normalize_legacy_status("totally-made-up") == "totally-made-up"


def test_normalize_stripped_whitespace(sm):
    assert sm.normalize_legacy_status("  pending  ") == "pending"


# ── is_valid / is_terminal / is_resumable ─────────────────────────────────


def test_is_valid_status_canonical(sm):
    for value in sm.canonical_values():
        assert sm.is_valid_status(value)


def test_is_valid_status_legacy_aliases(sm):
    for value in ("awaiting-input", "rate-limited", "paused"):
        assert sm.is_valid_status(value)


def test_is_valid_status_unknown(sm):
    assert not sm.is_valid_status("bogus")


def test_is_terminal(sm):
    assert sm.is_terminal("completed")
    assert sm.is_terminal("deleted")
    assert not sm.is_terminal("running")
    assert not sm.is_terminal("stopped")
    assert not sm.is_terminal("awaiting_input")


def test_is_resumable(sm):
    for value in ("stopped", "awaiting_input", "rate_limited", "error"):
        assert sm.is_resumable(value)
    for value in ("pending", "running", "completed", "deleted"):
        assert not sm.is_resumable(value)


def test_is_resumable_accepts_legacy_forms(sm):
    assert sm.is_resumable("awaiting-input")
    assert sm.is_resumable("rate-limited")
    assert sm.is_resumable("paused")


# ── validate_transition ────────────────────────────────────────────────────


def test_valid_transition_pending_to_running(sm):
    sm.validate_transition("pending", "running")


def test_valid_transition_running_to_awaiting_input(sm):
    sm.validate_transition("running", "awaiting_input")


def test_valid_transition_awaiting_input_to_pending(sm):
    sm.validate_transition("awaiting_input", "pending")


def test_valid_transition_stopped_to_pending(sm):
    sm.validate_transition("stopped", "pending")


def test_valid_transition_error_to_pending(sm):
    sm.validate_transition("error", "pending")


def test_valid_transition_any_to_deleted(sm):
    for origin in ("pending", "running", "awaiting_input", "rate_limited",
                   "stopped", "completed", "error"):
        sm.validate_transition(origin, "deleted")


def test_idempotent_transition_same_to_same_allowed(sm):
    for value in sm.canonical_values():
        sm.validate_transition(value, value)


def test_invalid_transition_deleted_is_terminal(sm):
    with pytest.raises(sm.InvalidTransition):
        sm.validate_transition("deleted", "running")


def test_invalid_transition_completed_only_to_deleted(sm):
    with pytest.raises(sm.InvalidTransition):
        sm.validate_transition("completed", "running")
    with pytest.raises(sm.InvalidTransition):
        sm.validate_transition("completed", "pending")
    # Only delete is allowed.
    sm.validate_transition("completed", "deleted")


def test_invalid_transition_to_unknown_target(sm):
    with pytest.raises(sm.InvalidTransition):
        sm.validate_transition("running", "bogus")


def test_validate_transition_accepts_legacy_forms(sm):
    # Legacy source.
    sm.validate_transition("awaiting-input", "running")
    # Legacy target.
    sm.validate_transition("running", "awaiting-input")
    # Both legacy.
    sm.validate_transition("rate-limited", "pending")
    # Paused as legacy alias for stopped.
    sm.validate_transition("paused", "pending")


def test_invalid_transition_attrs_populated(sm):
    with pytest.raises(sm.InvalidTransition) as exc_info:
        sm.validate_transition("completed", "running")
    err = exc_info.value
    assert err.current == "completed"
    assert err.target == "running"


# ── valid_targets ─────────────────────────────────────────────────────────


def test_valid_targets_pending(sm):
    targets = sm.valid_targets("pending")
    assert "running" in targets
    assert "stopped" in targets
    assert "deleted" in targets


def test_valid_targets_deleted_is_empty(sm):
    assert sm.valid_targets("deleted") == frozenset()


def test_valid_targets_legacy_source(sm):
    # Legacy alias must resolve to canonical table.
    assert sm.valid_targets("awaiting-input") == sm.valid_targets("awaiting_input")


# ── marker_suffix ─────────────────────────────────────────────────────────


def test_marker_suffix_every_canonical_state_has_mapping(sm):
    for value in sm.canonical_values():
        # Except deleted (empty suffix), every state has a non-empty suffix.
        if value == "deleted":
            assert sm.marker_suffix(value) == ""
        else:
            assert sm.marker_suffix(value) != ""


def test_marker_suffix_running(sm):
    assert sm.marker_suffix("running") == " · ▶"


def test_marker_suffix_awaiting_input(sm):
    assert sm.marker_suffix("awaiting_input") == " · ⏸"


def test_marker_suffix_rate_limited(sm):
    assert sm.marker_suffix("rate_limited") == " · ⏳"


def test_marker_suffix_stopped(sm):
    assert sm.marker_suffix("stopped") == " · ⏹"


def test_marker_suffix_completed(sm):
    assert sm.marker_suffix("completed") == " · ✅"


def test_marker_suffix_error(sm):
    assert sm.marker_suffix("error") == " · ❌"


def test_marker_suffix_accepts_legacy(sm):
    assert sm.marker_suffix("awaiting-input") == " · ⏸"
    assert sm.marker_suffix("paused") == " · ⏹"
