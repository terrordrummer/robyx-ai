"""Spec 006 FR-021 — drain-on-close with per-task timeout.

Tests the bounded-wait behaviour when a continuous task's parent
workspace is closed (or the task is being deleted) while a step is
still executing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


@pytest.fixture
def sched_mod():
    import scheduler  # type: ignore
    return scheduler


async def test_drain_when_no_lock_returns_not_running(
    sched_mod, tmp_path, monkeypatch,
):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue)

    result = await sched_mod.drain_and_cancel_continuous_task(
        "t", reason="test", drain_timeout_seconds=5,
    )
    assert result["drained"] is False
    assert result["timeout"] is False
    assert result["waited_seconds"] == 0.0


async def test_drain_succeeds_when_pid_exits_naturally(
    sched_mod, tmp_path, monkeypatch,
):
    """If the subprocess is already gone, drain returns immediately."""
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text('[{"name":"t","type":"continuous","status":"pending","thread_id":"9999"}]')
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue)

    # Write a lock file pointing at an already-dead pid.
    (tmp_path / "t").mkdir()
    sched_mod._write_lock_file(tmp_path / "t" / "lock", 99999)

    with patch("process.is_pid_alive", return_value=False):
        result = await sched_mod.drain_and_cancel_continuous_task(
            "t", reason="test", drain_timeout_seconds=5,
        )
    assert result["timeout"] is False


async def test_drain_reads_task_specific_timeout(
    sched_mod, tmp_path, monkeypatch,
):
    """If drain_timeout_seconds is stored in state and no override given,
    that value is used.
    """
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text("[]")
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue)

    state = cont.create_continuous_task(
        name="custom-drain",
        parent_workspace="p",
        program={"objective": "x"},
        thread_id=1,
        branch="b",
        work_dir="/tmp",
    )
    state["drain_timeout_seconds"] = 120
    cont.save_state(cont.state_file_path("custom-drain"), state)

    # No subprocess → drain returns immediately but the configured
    # timeout is still journaled.
    result = await sched_mod.drain_and_cancel_continuous_task(
        "custom-drain", reason="test",
    )
    assert result["drained"] is False

    # Drain_started event payload reflects 120s.
    import events as events_mod
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="custom-drain")
    drain_started = [e for e in entries if e["event_type"] == "drain_started"]
    assert len(drain_started) == 1
    assert drain_started[0]["payload"]["timeout_seconds"] == 120


async def test_drain_journals_started_and_completed(
    sched_mod, tmp_path, monkeypatch,
):
    import events as events_mod
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text("[]")
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue)

    await sched_mod.drain_and_cancel_continuous_task(
        "x", reason="test close", drain_timeout_seconds=5,
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="x")
    types = {e["event_type"] for e in entries}
    assert "drain_started" in types
    assert "drain_completed" in types


async def test_drain_timeout_override_takes_precedence(
    sched_mod, tmp_path, monkeypatch,
):
    """Explicit drain_timeout_seconds arg overrides the state value."""
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    queue = tmp_path / "queue.json"
    queue.write_text("[]")
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", queue)

    state = cont.create_continuous_task(
        name="override",
        parent_workspace="p",
        program={"objective": "x"},
        thread_id=1,
        branch="b",
        work_dir="/tmp",
    )
    state["drain_timeout_seconds"] = 3600  # 1 h in state
    cont.save_state(cont.state_file_path("override"), state)

    await sched_mod.drain_and_cancel_continuous_task(
        "override", drain_timeout_seconds=60,  # override to 60s
    )
    import events as events_mod
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = events_mod.query(since, task_name="override")
    drain_started = [e for e in entries if e["event_type"] == "drain_started"]
    assert len(drain_started) == 1
    # Override wins.
    assert drain_started[0]["payload"]["timeout_seconds"] == 60


async def test_drain_timeout_macro_attr_accepted(monkeypatch, tmp_path):
    import config as cfg
    work_dir = tmp_path / "workspace" / "draintest"
    work_dir.mkdir(parents=True)
    """`[CONTINUOUS_PROGRAM]` JSON payload with drain_timeout_seconds
    is honoured by continuous_macro dispatch.
    """
    import continuous_macro as mod

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "name": kwargs["name"],
            "display_name": kwargs["name"],
            "thread_id": 1,
            "branch": "b",
            "versioning": "git-branch",
            "state_file": "x",
            "plan_path": "y",
            "type": "continuous",
        }

    ctx = mod.ApplyContext(
        agent=type("A", (), {"name": "robyx"})(),
        thread_id=42,
        chat_id=-100,
        platform=None,
        manager=None,
        is_executive=True,
        create_continuous_workspace=fake_create,
    )
    text = """
[CREATE_CONTINUOUS name="draintest" work_dir="%s"]""" % work_dir
    text += """
[CONTINUOUS_PROGRAM]
{"objective": "test", "success_criteria": ["c1"], "constraints": [],
 "checkpoint_policy": "on-demand", "context": "x",
 "first_step": {"number": 1, "description": "begin"},
 "drain_timeout_seconds": 7200}
[/CONTINUOUS_PROGRAM]
"""
    out, outcomes = await mod.apply_continuous_macros(text, ctx)
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "intercepted", (
        "rejected: %s / %s" % (outcomes[0].reason, outcomes[0].detail)
    )
    assert captured.get("drain_timeout_seconds") == 7200


async def test_drain_timeout_out_of_range_ignored(monkeypatch, tmp_path):
    work_dir = tmp_path / "workspace" / "oob"
    work_dir.mkdir(parents=True)
    import continuous_macro as mod

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "name": kwargs["name"], "display_name": kwargs["name"],
            "thread_id": 1, "branch": "b", "versioning": "git-branch",
            "state_file": "x", "plan_path": "y", "type": "continuous",
        }

    ctx = mod.ApplyContext(
        agent=type("A", (), {"name": "robyx"})(),
        thread_id=42, chat_id=-100, platform=None, manager=None,
        is_executive=True, create_continuous_workspace=fake_create,
    )
    text = """
[CREATE_CONTINUOUS name="oob" work_dir="%s"]""" % work_dir
    text += """
[CONTINUOUS_PROGRAM]
{"objective": "x", "success_criteria": ["c1"], "constraints": [],
 "checkpoint_policy": "on-demand", "context": "y",
 "first_step": {"number": 1, "description": "begin"},
 "drain_timeout_seconds": 999999}
[/CONTINUOUS_PROGRAM]
"""
    out, outcomes = await mod.apply_continuous_macros(text, ctx)
    assert captured.get("drain_timeout_seconds") is None  # clamped → ignored
