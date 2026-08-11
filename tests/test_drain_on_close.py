"""Spec 006 FR-021 — drain-on-close with per-task timeout.

Tests the bounded-wait behaviour when a continuous task's parent
workspace is closed (or the task is being deleted) while a step is
still executing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
    """The chat-facing duration attribute is parsed and dispatched."""
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
[CREATE_CONTINUOUS name="draintest" work_dir="%s" drain_timeout="1h"]""" % work_dir
    text += """
[CONTINUOUS_PROGRAM]
{"objective": "test", "success_criteria": ["c1"], "constraints": [],
 "checkpoint_policy": "on-demand", "context": "x",
 "first_step": {"number": 1, "description": "begin"}}
[/CONTINUOUS_PROGRAM]
"""
    out, outcomes = await mod.apply_continuous_macros(text, ctx)
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "intercepted", (
        "rejected: %s / %s" % (outcomes[0].reason, outcomes[0].detail)
    )
    assert captured.get("drain_timeout_seconds") == 3600


async def test_drain_timeout_numeric_json_compatibility(monkeypatch, tmp_path):
    """The pre-attribute JSON field remains compatible within the new cap."""
    work_dir = tmp_path / "workspace" / "json-compat"
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
[CREATE_CONTINUOUS name="json-compat" work_dir="%s"]
[CONTINUOUS_PROGRAM]
{"objective": "test", "success_criteria": ["c1"], "constraints": [],
 "checkpoint_policy": "on-demand", "context": "x",
 "first_step": {"number": 1, "description": "begin"},
 "drain_timeout_seconds": 7200}
[/CONTINUOUS_PROGRAM]
""" % work_dir

    _, outcomes = await mod.apply_continuous_macros(text, ctx)

    assert outcomes[0].outcome == "intercepted"
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


async def test_workspace_close_waits_for_delivery_with_visible_marker(
    sched_mod,
    tmp_path,
    monkeypatch,
):
    import continuous as cont
    import events
    import orphan_tracker
    from runtime_supervisor import get_runtime_supervisor
    from scheduled_delivery import start_task_delivery_watch

    supervisor = get_runtime_supervisor()
    if supervisor.closing:
        supervisor.reset_for_tests()
    assert supervisor.process_count == 0
    monkeypatch.setattr(orphan_tracker, "register", MagicMock())
    monkeypatch.setattr(orphan_tracker, "unregister", MagicMock())
    monkeypatch.setattr(events, "append", MagicMock())
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sched_mod, "QUEUE_FILE", tmp_path / "queue.json")

    state = cont.create_continuous_task(
        name="closing-child",
        parent_workspace="parent",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/closing-child",
        work_dir=str(tmp_path),
    )
    state["status"] = "running"
    state["dedicated_thread_id"] = 900
    cont.save_state(cont.state_file_path("closing-child"), state)
    (tmp_path / "queue.json").write_text(
        '[{"name":"closing-child","type":"continuous",'
        '"status":"pending","thread_id":"900"}]'
    )
    task_dir = tmp_path / "closing-child"
    task_dir.mkdir()
    output_log = task_dir / "output.log"
    output_log.write_text("final result")
    lock_file = task_dir / "lock"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(0.15)",
        start_new_session=True,
    )
    sched_mod._write_lock_file(lock_file, proc.pid)
    platform = AsyncMock()
    platform.send_to_channel = AsyncMock(return_value=True)
    platform.max_message_length = 4000
    backend = MagicMock()
    backend.parse_response.return_value = "final result"
    watcher = start_task_delivery_watch(
        {
            "name": "closing-child",
            "type": "continuous",
            "thread_id": "900",
        },
        proc,
        output_log,
        lock_file,
        platform,
        backend,
        MagicMock(),
    )

    result = await sched_mod.drain_and_cancel_continuous_task(
        "closing-child",
        reason="workspace closed",
        drain_timeout_seconds=2,
    )
    await watcher

    assert result["drained"] is True
    delivered = platform.send_to_channel.await_args.args[1]
    assert "workspace closed" in delivered
    fresh = cont.load_state(cont.state_file_path("closing-child"))
    assert "delivery_state_override" not in fresh
    assert not lock_file.exists()


async def test_delete_mid_drain_journals_bounded_chat_body(
    tmp_path,
    monkeypatch,
):
    """T061: a delete marker wins over topic delivery without raw leakage."""
    import continuous as cont
    import events
    from scheduled_delivery import deliver_task_output

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    state = cont.create_continuous_task(
        name="delete-mid-drain",
        parent_workspace="parent",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/delete-mid-drain",
        work_dir=str(tmp_path),
    )
    state["status"] = "running"
    state["dedicated_thread_id"] = 901
    state["delete_in_progress_at"] = datetime.now(timezone.utc).isoformat()
    cont.save_state(cont.state_file_path("delete-mid-drain"), state)

    output_log = tmp_path / "output.log"
    output_log.write_text("RAW_DIAGNOSTIC_MUST_NOT_BE_JOURNALED")
    backend = MagicMock()
    backend.parse_response.return_value = (
        "[STATUS internal]\n"
        "user-facing result "
        + ("é" * 5000)
        + '\n[CREATE_CONTINUOUS name="leak" work_dir="/tmp"]'
    )
    platform = AsyncMock()
    platform.send_to_channel = AsyncMock(return_value=True)
    platform.max_message_length = 4000

    delivered = await deliver_task_output(
        {
            "name": "delete-mid-drain",
            "type": "continuous",
            "thread_id": "901",
        },
        output_log,
        platform,
        backend,
        0,
        MagicMock(),
    )

    assert delivered is True
    platform.send_to_channel.assert_not_awaited()
    entries = events.query(
        datetime.now(timezone.utc) - timedelta(minutes=1),
        task_name="delete-mid-drain",
        event_type="step_complete",
    )
    assert len(entries) == 1
    payload = entries[0]["payload"]
    assert payload["delivery"] == "drain_during_delete"
    assert payload["body_truncated"] is True
    assert len(payload["body"].encode("utf-8")) <= 8 * 1024
    assert "RAW_DIAGNOSTIC" not in payload["body"]
    assert "STATUS" not in payload["body"]
    assert "CREATE_CONTINUOUS" not in payload["body"]
    fresh = cont.load_state(cont.state_file_path("delete-mid-drain"))
    assert fresh["drain_delete_reference_pending"] is True
    assert fresh["drain_delete_output_recorded_at"] is not None


async def test_delete_posts_single_reference_as_last_line_before_archive(
    tmp_path,
    monkeypatch,
):
    """The delete transaction owns reference ordering after watcher drain."""
    import continuous as cont
    import lifecycle_macros as lifecycle
    import scheduler as sched

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "queue.json")
    state = cont.create_continuous_task(
        name="ordered-delete",
        parent_workspace="parent",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/ordered-delete",
        work_dir=str(tmp_path),
    )
    state["status"] = "running"
    state["dedicated_thread_id"] = 902
    cont.save_state(cont.state_file_path("ordered-delete"), state)
    (tmp_path / "queue.json").write_text(json.dumps([{
        "name": "ordered-delete",
        "type": "continuous",
        "status": "pending",
        "thread_id": "902",
    }]))

    async def fake_drain(name, reason, fallback):
        fresh = cont.load_state(cont.state_file_path(name)) or fallback
        assert fresh["delete_in_progress_at"] is not None
        fresh["drain_delete_output_recorded_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        fresh["drain_delete_reference_pending"] = True
        cont.save_state(cont.state_file_path(name), fresh)
        return fresh

    monkeypatch.setattr(lifecycle, "_drain_for_lifecycle", fake_drain)
    order = []

    async def send_to_channel(_thread_id, body, parse_mode=None):
        order.append(("send", body))
        return True

    async def archive_topic(_thread_id, _display_name):
        order.append(("archive", None))
        return True

    platform = MagicMock()
    platform.send_to_channel = AsyncMock(side_effect=send_to_channel)
    platform.archive_topic = AsyncMock(side_effect=archive_topic)
    platform.unpin_message = AsyncMock(return_value=True)
    ctx = lifecycle.DispatchContext(
        chat_id=-100,
        thread_id=42,
        platform=platform,
    )

    result = await lifecycle._delete_task(
        {
            "entry": {
                "name": "ordered-delete",
                "type": "continuous",
                "status": "pending",
            },
            "state": state,
        },
        ctx,
    )

    assert "eliminato" in result
    assert order[-1] == ("archive", None)
    reference_messages = [
        body for kind, body in order
        if kind == "send" and "drain output recorded in journal" in body
    ]
    assert len(reference_messages) == 1
    assert order[-2] == ("send", reference_messages[0])
    fresh = cont.load_state(cont.state_file_path("ordered-delete"))
    assert fresh["status"] == "deleted"
    assert fresh["drain_delete_reference_pending"] is False
    assert fresh["drain_delete_reference_sent_at"] is not None


async def test_already_archived_drain_output_is_journal_only(
    tmp_path,
    monkeypatch,
):
    """Once archive completed the full-body event is the sole delivery."""
    import continuous as cont
    import events
    from scheduled_delivery import deliver_task_output

    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")
    state = cont.create_continuous_task(
        name="already-archived",
        parent_workspace="parent",
        program={"objective": "x"},
        thread_id=42,
        branch="continuous/already-archived",
        work_dir=str(tmp_path),
    )
    state["status"] = "deleted"
    state["archived_at"] = datetime.now(timezone.utc).isoformat()
    state["dedicated_thread_id"] = 903
    cont.save_state(cont.state_file_path("already-archived"), state)
    output_log = tmp_path / "archived-output.log"
    output_log.write_text("raw wrapper")
    backend = MagicMock()
    backend.parse_response.return_value = "final safe body"
    platform = AsyncMock()
    platform.send_to_channel = AsyncMock(return_value=True)

    assert await deliver_task_output(
        {"name": "already-archived", "type": "continuous", "thread_id": 903},
        output_log,
        platform,
        backend,
        0,
        MagicMock(),
    ) is True

    platform.send_to_channel.assert_not_awaited()
    entries = events.query(
        datetime.now(timezone.utc) - timedelta(minutes=1),
        task_name="already-archived",
        event_type="step_complete",
    )
    assert len(entries) == 1
    assert entries[0]["outcome"] == "archived"
    assert entries[0]["payload"]["body"] == "final safe body"
    fresh = cont.load_state(cont.state_file_path("already-archived"))
    assert fresh["drain_delete_reference_pending"] is False
