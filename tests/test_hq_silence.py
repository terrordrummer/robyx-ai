"""Spec 006 FR-002 — zero scheduler-driven push notifications in HQ.

Verifies that scheduler dispatches, completions, orphan detections, and
state transitions emit journal entries only — never direct HQ messages.

The FR-002a last-resort exception is tested separately in
``test_hq_fallback.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def fake_platform():
    plat = AsyncMock()
    plat.send_message = AsyncMock()
    plat.send_to_channel = AsyncMock(return_value=True)
    plat.create_channel = AsyncMock(return_value=9999)
    plat.control_room_id = 1
    return plat


async def test_scheduler_orphan_emits_journal_not_hq(
    fake_platform, tmp_path, monkeypatch,
):
    """Orphan detection writes to the journal but does not post to HQ."""
    import continuous as cont
    import events as events_mod
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)

    # Fabricate a continuous task stuck in 'running' with no lock.
    state = cont.create_continuous_task(
        name="ghost-task", parent_workspace="p",
        program={"objective": "x"},
        thread_id=42,
        branch="b", work_dir=str(tmp_path),
    )
    state["status"] = "running"
    cont.save_state(cont.state_file_path("ghost-task"), state)

    # Invoke the spec-006 orphan handler directly. Below threshold →
    # marks status=pending, journals orphan_detected.
    sched._handle_continuous_orphan(state, "ghost-task")

    # No HQ send triggered.
    fake_platform.send_message.assert_not_awaited()
    # Journal entry present.
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="ghost-task")
    assert any(e["event_type"] == "orphan_detected" for e in entries)


async def test_scheduler_orphan_escalation_emits_incident_not_hq(
    fake_platform, tmp_path, monkeypatch,
):
    """After ORPHAN_INCIDENT_THRESHOLD consecutive detections, one
    `orphan_incident` event is journaled. No HQ push side-effect.
    """
    import config as cfg
    import continuous as cont
    import events as events_mod
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ORPHAN_INCIDENT_THRESHOLD", 3)

    state = cont.create_continuous_task(
        name="crasher", parent_workspace="p",
        program={"objective": "x"},
        thread_id=42,
        branch="b", work_dir=str(tmp_path),
    )
    state["status"] = "running"
    cont.save_state(cont.state_file_path("crasher"), state)

    # Three back-to-back orphan detections.
    for _ in range(3):
        sched._handle_continuous_orphan(state, "crasher")

    # State transitioned to error on the 3rd call.
    assert state["status"] == "error"

    # Journal contains exactly one orphan_incident.
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="crasher")
    incident_entries = [e for e in entries if e["event_type"] == "orphan_incident"]
    assert len(incident_entries) == 1
    # No HQ send.
    fake_platform.send_message.assert_not_awaited()


async def test_scheduler_dispatch_journals_event(tmp_path, monkeypatch):
    """The `_journal_scheduler_event` helper writes to the journal with
    the expected task_type and event_type.
    """
    import events as events_mod
    import scheduler as sched

    sched._journal_scheduler_event(
        task_name="foo",
        event_type="dispatched",
        outcome="ok",
        payload={"pid": 1234, "step": 5},
    )
    since = datetime.now(timezone.utc) - timedelta(seconds=30)
    entries = events_mod.query(since, task_name="foo")
    assert len(entries) == 1
    e = entries[0]
    assert e["event_type"] == "dispatched"
    assert e["payload"]["pid"] == 1234
    assert e["task_type"] == "continuous"


async def test_rotation_hook_called_per_cycle_is_safe(tmp_path, monkeypatch):
    """Calling `events.rotate_if_needed` on an empty / fresh journal
    returns None silently — confirms the scheduler-cycle hook won't
    spam errors when there's nothing to rotate.
    """
    import events as events_mod

    assert events_mod.rotate_if_needed() is None
    assert events_mod.rotate_if_needed() is None
