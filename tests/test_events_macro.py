"""Tests for [GET_EVENTS] macro grammar + handler (spec 006 US1).

Contract: ``specs/006-continuous-task-robustness/contracts/events-macro.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ── Pattern ──────────────────────────────────────────────────────────────


def test_pattern_matches_minimal():
    from ai_invoke import GET_EVENTS_PATTERN
    m = GET_EVENTS_PATTERN.search('[GET_EVENTS since="30m"]')
    assert m is not None


def test_pattern_matches_all_attrs():
    from ai_invoke import GET_EVENTS_PATTERN
    m = GET_EVENTS_PATTERN.search(
        '[GET_EVENTS since="2h" task="x" type="dispatched" limit="50"]',
    )
    assert m is not None


def test_pattern_matches_no_attrs():
    """Bare [GET_EVENTS] still matches — handler will produce an error token."""
    from ai_invoke import GET_EVENTS_PATTERN
    m = GET_EVENTS_PATTERN.search("[GET_EVENTS]")
    assert m is not None


def test_pattern_multiple_occurrences():
    from ai_invoke import GET_EVENTS_PATTERN
    text = '[GET_EVENTS since="1h"] ... [GET_EVENTS since="2h" task="x"]'
    matches = list(GET_EVENTS_PATTERN.finditer(text))
    assert len(matches) == 2


# ── _handle_get_events ──────────────────────────────────────────────────


@pytest.fixture
def handler(monkeypatch):
    """Build a handlers dict and return the _handle_get_events callable."""
    from unittest.mock import MagicMock

    import handlers as handlers_mod

    manager = MagicMock()
    backend = MagicMock()
    h = handlers_mod.make_handlers(manager, backend)
    return h["_handle_get_events"]


@pytest.fixture
def seeded_events(monkeypatch):
    """Seed the journal with a few events for query assertions."""
    import events as events_mod

    events_mod.append(
        task_name="zeus-research",
        task_type="continuous",
        event_type="dispatched",
        outcome="ok",
        payload={"step": 1},
    )
    events_mod.append(
        task_name="zeus-research",
        task_type="continuous",
        event_type="step_complete",
        outcome="awaiting_input",
        payload={"step": 1},
    )
    events_mod.append(
        task_name="other-task",
        task_type="continuous",
        event_type="dispatched",
        outcome="ok",
    )


async def test_handler_no_macro_returns_unchanged(handler):
    out = await handler("just a normal response", "robyx")
    assert out == "just a normal response"


async def test_handler_substitutes_with_table(handler, seeded_events):
    response = 'Here is history: [GET_EVENTS since="1h"]'
    out = await handler(response, "robyx")
    # The raw macro token is stripped (the error message itself contains
    # the bracketed phrase "[GET_EVENTS error: …]" which is renderable
    # markdown — distinct from an unprocessed macro).
    from ai_invoke import GET_EVENTS_PATTERN
    assert GET_EVENTS_PATTERN.search(out) is None
    # Markdown table present.
    assert "| ts | task | type | outcome |" in out
    # Our three seeded events visible.
    assert "zeus-research" in out
    assert "other-task" in out


async def test_handler_filters_by_task(handler, seeded_events):
    response = '[GET_EVENTS since="1h" task="zeus-research"]'
    out = await handler(response, "robyx")
    assert "zeus-research" in out
    assert "other-task" not in out


async def test_handler_filters_by_event_type(handler, seeded_events):
    response = '[GET_EVENTS since="1h" type="step_complete"]'
    out = await handler(response, "robyx")
    assert "step_complete" in out
    assert "dispatched" not in out


async def test_handler_error_on_missing_since(handler):
    response = '[GET_EVENTS task="x"]'
    out = await handler(response, "robyx")
    assert "INVALID_DURATION" in out
    from ai_invoke import GET_EVENTS_PATTERN
    assert GET_EVENTS_PATTERN.search(out) is None
    # The raw macro token is stripped (the error message itself contains
    # the bracketed phrase "[GET_EVENTS error: …]" which is renderable
    # markdown — distinct from an unprocessed macro).
    from ai_invoke import GET_EVENTS_PATTERN
    assert GET_EVENTS_PATTERN.search(out) is None  # still stripped


async def test_handler_error_on_bad_duration(handler):
    response = '[GET_EVENTS since="not-a-duration"]'
    out = await handler(response, "robyx")
    assert "INVALID_DURATION" in out
    from ai_invoke import GET_EVENTS_PATTERN
    assert GET_EVENTS_PATTERN.search(out) is None


async def test_handler_error_on_bad_limit(handler):
    response = '[GET_EVENTS since="1h" limit="abc"]'
    out = await handler(response, "robyx")
    assert "INVALID_LIMIT" in out


async def test_handler_limit_range_rejects_out_of_bounds(handler):
    # limit=0 is out of [1, 1000]
    response = '[GET_EVENTS since="1h" limit="0"]'
    out = await handler(response, "robyx")
    assert "INVALID_LIMIT" in out
    # limit=1001 too
    response = '[GET_EVENTS since="1h" limit="1001"]'
    out = await handler(response, "robyx")
    assert "INVALID_LIMIT" in out


async def test_handler_accepts_iso_8601_since(handler, seeded_events):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    response = '[GET_EVENTS since="%s"]' % past
    out = await handler(response, "robyx")
    assert "zeus-research" in out


async def test_handler_accepts_iso_z_suffix(handler, seeded_events):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    response = '[GET_EVENTS since="%s"]' % past
    out = await handler(response, "robyx")
    # Should parse without INVALID_DURATION.
    assert "INVALID_DURATION" not in out


async def test_handler_empty_window_returns_no_events_note(handler):
    response = '[GET_EVENTS since="1s"]'
    # Journal is empty (fresh tmp env); handler should render a friendly note.
    out = await handler(response, "robyx")
    assert "No events" in out or "(0 entries)" in out


async def test_handler_multiple_macros_all_substituted(handler, seeded_events):
    response = (
        'First: [GET_EVENTS since="1h" task="zeus-research"] '
        'Second: [GET_EVENTS since="1h" task="other-task"]'
    )
    out = await handler(response, "robyx")
    # Both macros stripped; both data sets present.
    # The raw macro token is stripped (the error message itself contains
    # the bracketed phrase "[GET_EVENTS error: …]" which is renderable
    # markdown — distinct from an unprocessed macro).
    from ai_invoke import GET_EVENTS_PATTERN
    assert GET_EVENTS_PATTERN.search(out) is None
    assert "zeus-research" in out
    assert "other-task" in out


async def test_pattern_registered_in_executive_markers():
    """The GET_EVENTS pattern is stripped for non-executive agents (safety)."""
    from handlers import _EXECUTIVE_MARKERS

    names = {name for name, _ in _EXECUTIVE_MARKERS}
    assert "GET_EVENTS" in names
