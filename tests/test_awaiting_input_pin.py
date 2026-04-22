"""Spec 006 FR-010 / FR-011 — awaiting-input pin + 24h reminder.

Tests the scheduler's awaiting-reminder loop and the continuous.py
pin/unpin helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_platform_spec006():
    plat = AsyncMock()
    plat.send_message = AsyncMock()
    plat.send_to_channel = AsyncMock(return_value=True)
    plat.create_channel = AsyncMock(return_value=8888)
    plat.edit_topic_title = AsyncMock(return_value=True)
    plat.pin_message = AsyncMock(return_value=True)
    plat.unpin_message = AsyncMock(return_value=True)
    plat.control_room_id = 1
    plat.max_message_length = 4000
    return plat


# ── awaiting_since_ts lifecycle ────────────────────────────────────────────


def test_set_awaiting_input_stamps_since_ts():
    """`set_awaiting_input` records an ISO-8601 awaiting_since_ts and
    clears any stale reminder timestamp.
    """
    import continuous as cont

    state = {"status": "running", "updated_at": ""}
    state["awaiting_reminder_sent_ts"] = "stale"  # leftover from prior episode
    cont.set_awaiting_input(state, "which approach?")
    assert state["status"] == "awaiting_input"
    assert state["awaiting_since_ts"] is not None
    assert state["awaiting_reminder_sent_ts"] is None
    # Parseable.
    datetime.fromisoformat(state["awaiting_since_ts"])


def test_resume_task_clears_all_awaiting_fields():
    """`resume_task` wipes every awaiting_* field so the next episode
    starts fresh.
    """
    import continuous as cont

    state = {
        "status": "awaiting_input",
        "rate_limited_until": None,
        "awaiting_question": "pick",
        "awaiting_since_ts": "2026-01-01T00:00:00+00:00",
        "awaiting_pinned_msg_id": 42,
        "awaiting_reminder_sent_ts": "2026-01-02T00:00:00+00:00",
        "updated_at": "",
    }
    cont.resume_task(state)
    assert state["status"] == "pending"
    assert state.get("awaiting_question") is None
    assert state["awaiting_since_ts"] is None
    assert state["awaiting_pinned_msg_id"] is None
    assert state["awaiting_reminder_sent_ts"] is None


# ── 24h reminder loop ─────────────────────────────────────────────────────


async def test_reminder_posts_once_after_threshold(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    """A task in awaiting_input for longer than AWAITING_REMINDER_SECONDS
    receives exactly one reminder; subsequent cycles do not duplicate.
    """
    import config as cfg
    import continuous as cont
    import events as events_mod
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(cfg, "AWAITING_REMINDER_SECONDS", 1)
    monkeypatch.setattr(cfg, "CONTINUOUS_DIR", tmp_path / "continuous")

    # Build a task already past the reminder threshold.
    state = cont.create_continuous_task(
        name="reminder-test",
        parent_workspace="p",
        program={"objective": "x"},
        thread_id=42,
        branch="b",
        work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 8888
    state["status"] = "awaiting_input"
    state["awaiting_question"] = "pick topic"
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state["awaiting_since_ts"] = past
    cont.save_state(cont.state_file_path("reminder-test"), state)

    # First run — reminder fires.
    posted = await sched._dispatch_awaiting_reminders(
        mock_platform_spec006, default_chat_id=-100,
    )
    assert posted == 1
    mock_platform_spec006.send_to_channel.assert_awaited_once()
    called = mock_platform_spec006.send_to_channel.call_args
    assert called.args[0] == 8888  # dedicated topic
    assert "pick topic" in called.args[1]

    # Journal entry present.
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="reminder-test")
    reminder_events = [
        e for e in entries if e["event_type"] == "awaiting_reminder_sent"
    ]
    assert len(reminder_events) == 1

    # Second run — no duplicate (awaiting_reminder_sent_ts is set).
    mock_platform_spec006.send_to_channel.reset_mock()
    posted = await sched._dispatch_awaiting_reminders(
        mock_platform_spec006, default_chat_id=-100,
    )
    assert posted == 0
    mock_platform_spec006.send_to_channel.assert_not_awaited()


async def test_reminder_skipped_if_not_yet_threshold(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    import config as cfg
    import continuous as cont
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(cfg, "AWAITING_REMINDER_SECONDS", 86400)  # 24 h
    monkeypatch.setattr(cfg, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="fresh-awaiting",
        parent_workspace="p",
        program={"objective": "x"},
        thread_id=42,
        branch="b",
        work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 1111
    state["status"] = "awaiting_input"
    state["awaiting_question"] = "q?"
    # Just now.
    state["awaiting_since_ts"] = datetime.now(timezone.utc).isoformat()
    cont.save_state(cont.state_file_path("fresh-awaiting"), state)

    posted = await sched._dispatch_awaiting_reminders(
        mock_platform_spec006, default_chat_id=-100,
    )
    assert posted == 0
    mock_platform_spec006.send_to_channel.assert_not_awaited()


async def test_resume_after_reminder_restarts_episode(
    tmp_path, monkeypatch, mock_platform_spec006,
):
    """After resume, a subsequent awaiting_input transition resets the
    reminder state so a fresh 24h window applies.
    """
    import config as cfg
    import continuous as cont
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(cfg, "AWAITING_REMINDER_SECONDS", 1)
    monkeypatch.setattr(cfg, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="cycle-test",
        parent_workspace="p",
        program={"objective": "x"},
        thread_id=42,
        branch="b",
        work_dir="/tmp",
    )
    state["dedicated_thread_id"] = 2222
    # First episode.
    cont.set_awaiting_input(state, "q1?")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state["awaiting_since_ts"] = past
    cont.save_state(cont.state_file_path("cycle-test"), state)

    posted = await sched._dispatch_awaiting_reminders(
        mock_platform_spec006, default_chat_id=-100,
    )
    assert posted == 1

    # User resumes.
    cont.resume_task(state)
    # Second awaiting episode.
    cont.set_awaiting_input(state, "q2?")
    past2 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    state["awaiting_since_ts"] = past2
    cont.save_state(cont.state_file_path("cycle-test"), state)

    # Reminder_sent_ts was cleared by set_awaiting_input → fresh reminder fires.
    mock_platform_spec006.send_to_channel.reset_mock()
    posted = await sched._dispatch_awaiting_reminders(
        mock_platform_spec006, default_chat_id=-100,
    )
    assert posted == 1
