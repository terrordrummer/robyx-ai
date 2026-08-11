"""Spec 006 dedicated-topic recovery and exactly-once HQ fallback."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    import config
    import continuous
    import events
    import scheduler

    continuous_dir = tmp_path / "continuous"
    queue_file = tmp_path / "queue.json"
    monkeypatch.setattr(continuous, "CONTINUOUS_DIR", continuous_dir)
    monkeypatch.setattr(config, "CONTINUOUS_DIR", continuous_dir)
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(events, "append", MagicMock())

    def create(name="recover-me", *, status="awaiting_input", queued=True):
        state = continuous.create_continuous_task(
            name=name,
            parent_workspace="ops",
            program={"objective": "x"},
            thread_id=42,
            branch="continuous/%s" % name,
            work_dir=str(tmp_path),
        )
        state["status"] = status
        state["dedicated_thread_id"] = 111
        continuous.save_state(continuous.state_file_path(name), state)
        entries = []
        if queued:
            entries.append({
                "id": "queue-%s" % name,
                "name": name,
                "type": "continuous",
                "status": "pending",
                "thread_id": "111",
                "state_file": str(continuous.state_file_path(name)),
            })
        queue_file.write_text(json.dumps(entries))
        return state

    return create, continuous, scheduler, config


def _platform(*, create_result=222):
    platform = MagicMock()
    platform.create_channel = AsyncMock(return_value=create_result)
    platform.send_to_channel = AsyncMock(return_value=True)
    platform.send_message = AsyncMock(return_value={"message_id": 1})
    platform.control_room_id = 0
    return platform


@pytest.mark.asyncio
async def test_recreate_repoints_state_and_queue_and_replays_pending(recovery_env):
    create, continuous, scheduler, _ = recovery_env
    create()
    platform = _platform(create_result=222)
    from topic_recovery import recover_unreachable_topic

    result = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="TOPIC_ID_INVALID",
        event="awaiting_input",
        pending_delivery="please choose",
    )

    assert result.recreated_thread_id == 222
    assert result.pending_delivered is True
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["dedicated_thread_id"] == 222
    assert state["topic_unreachable_since_ts"] is None
    assert state["topic_pending_delivery"] is None
    queue = scheduler.load_queue()
    assert queue[0]["thread_id"] == "222"
    platform.send_to_channel.assert_awaited_once_with(
        222,
        "please choose",
        parse_mode="Markdown",
    )
    platform.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recreated_awaiting_topic_uses_message_ref_and_restores_pin(recovery_env, monkeypatch):
    create, continuous, _, _ = recovery_env
    create()

    class RefPlatform:
        async def create_channel(self, _name):
            return 223

        async def send_to_channel_with_ref(self, _channel_id, _text, **_kwargs):
            return {"message_id": 77}

    pin = AsyncMock(return_value=True)
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(continuous, "pin_awaiting_message", pin)
    monkeypatch.setattr(continuous, "update_topic_state_marker", marker)
    from topic_recovery import recover_unreachable_topic

    result = await recover_unreachable_topic(
        "recover-me",
        RefPlatform(),
        reason="topic deleted",
        event="awaiting_input",
        pending_delivery="please choose",
        hq_chat_id=-100,
    )

    assert result.pending_delivered is True
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["topic_pending_delivery"] is None
    assert state["topic_marker_status"] == "awaiting_input"
    pin.assert_awaited_once()
    assert pin.await_args.args[3] == 77
    marker.assert_awaited_once()


@pytest.mark.asyncio
async def test_actionable_failure_posts_hq_once_after_window(recovery_env, monkeypatch):
    create, continuous, _, config = recovery_env
    create(status="error")
    monkeypatch.setattr(config, "TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS", 10)
    platform = _platform(create_result=None)
    from topic_recovery import recover_unreachable_topic

    start = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    first = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="channel deleted",
        event="error",
        now_fn=lambda: start,
    )
    assert first.fallback_sent is False
    platform.send_message.assert_not_awaited()

    second = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="channel deleted",
        event="error",
        hq_chat_id=-100,
        now_fn=lambda: start + timedelta(seconds=11),
    )
    third = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="channel deleted",
        event="error",
        hq_chat_id=-100,
        now_fn=lambda: start + timedelta(seconds=12),
    )

    assert second.fallback_sent is True
    assert third.fallback_sent is False
    platform.send_message.assert_awaited_once()
    assert platform.send_message.await_args.args[0] == -100
    assert platform.send_message.await_args.kwargs["thread_id"] == 0
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["hq_fallback_sent"] is True
    assert state["topic_recovery_attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("send_result", [None, False])
async def test_failed_hq_send_is_not_marked_as_delivered(
    recovery_env,
    monkeypatch,
    send_result,
):
    create, continuous, _, config = recovery_env
    state = create(status="error")
    state["topic_unreachable_since_ts"] = "invalid timestamp"
    state["topic_recovery_attempts"] = 3
    state["topic_unreachable_event"] = "error"
    continuous.save_state(continuous.state_file_path("recover-me"), state)
    monkeypatch.setattr(config, "TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS", 0)
    platform = _platform(create_result=None)
    platform.send_message.return_value = send_result
    from topic_recovery import recover_unreachable_topic

    result = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="gone",
    )

    assert result.fallback_sent is False
    fresh = continuous.load_state(
        continuous.state_file_path("recover-me"),
    )
    if send_result is False:
        assert fresh["hq_fallback_sent"] is False
        assert fresh["hq_fallback_outcome"] == "not_delivered"
    else:
        assert fresh["hq_fallback_sent"] is True
        assert fresh["hq_fallback_outcome"] == "delivery_uncertain"


@pytest.mark.asyncio
async def test_failed_recreate_attempt_is_persisted_for_next_tick(recovery_env):
    create, continuous, _, _ = recovery_env
    create(status="error")
    platform = _platform()
    platform.create_channel.side_effect = RuntimeError("platform unavailable")
    from topic_recovery import recover_unreachable_topic

    result = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="gone",
        event="error",
    )

    assert result == type(result)()
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["topic_recovery_attempts"] == 1
    assert state["topic_unreachable_reason"] == "platform unavailable"


@pytest.mark.asyncio
async def test_routine_failure_never_posts_hq(recovery_env, monkeypatch):
    create, continuous, _, config = recovery_env
    create(status="pending")
    monkeypatch.setattr(config, "TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS", 0)
    platform = _platform(create_result=None)
    from topic_recovery import recover_unreachable_topic

    for _ in range(4):
        await recover_unreachable_topic(
            "recover-me",
            platform,
            reason="transient API failure",
            event=None,
        )
    platform.send_message.assert_not_awaited()
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["hq_fallback_sent"] is False


@pytest.mark.asyncio
async def test_successful_delivery_resets_episode_and_suppression(recovery_env):
    create, continuous, _, _ = recovery_env
    state = create()
    state["topic_unreachable_since_ts"] = datetime.now(timezone.utc).isoformat()
    state["topic_unreachable_reason"] = "gone"
    state["topic_unreachable_event"] = "awaiting_input"
    state["topic_recovery_attempts"] = 3
    state["topic_pending_delivery"] = "pending"
    state["hq_fallback_sent"] = True
    continuous.save_state(continuous.state_file_path("recover-me"), state)
    from topic_recovery import mark_topic_reachable

    await mark_topic_reachable("recover-me")
    fresh = continuous.load_state(continuous.state_file_path("recover-me"))
    assert fresh["topic_unreachable_since_ts"] is None
    assert fresh["topic_recovery_attempts"] == 0
    assert fresh["topic_pending_delivery"] is None
    assert fresh["hq_fallback_sent"] is False


@pytest.mark.asyncio
async def test_repoint_failure_restores_old_state(recovery_env):
    create, continuous, _, _ = recovery_env
    create(queued=False)
    platform = _platform(create_result=333)
    platform.archive_topic = AsyncMock(return_value=True)
    from topic_recovery import recover_unreachable_topic

    with pytest.raises(RuntimeError, match="active queue entry missing"):
        await recover_unreachable_topic(
            "recover-me",
            platform,
            reason="gone",
            event="error",
        )
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["dedicated_thread_id"] == 111
    assert state["topic_unreachable_since_ts"] is not None
    platform.archive_topic.assert_awaited_once_with(333, "recover-me")


@pytest.mark.asyncio
async def test_failed_replay_retries_same_recreated_topic_without_proliferation(
    recovery_env,
):
    create, continuous, _, _ = recovery_env
    create()
    platform = _platform(create_result=333)
    platform.send_to_channel.return_value = False
    from topic_recovery import recover_unreachable_topic

    first = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="old topic gone",
        event="awaiting_input",
        pending_delivery="choose",
    )
    assert first.recreated_thread_id == 333
    assert first.pending_delivered is False
    platform.send_to_channel.return_value = True

    second = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="replay failed",
        event="awaiting_input",
    )

    assert second.recreated_thread_id == 333
    assert second.pending_delivered is True
    platform.create_channel.assert_awaited_once()
    state = continuous.load_state(continuous.state_file_path("recover-me"))
    assert state["topic_recovery_candidate_thread_id"] is None
    assert state["topic_pending_delivery"] is None


@pytest.mark.asyncio
async def test_hq_claim_is_persisted_before_uncertain_external_send(
    recovery_env,
    monkeypatch,
):
    create, continuous, _, config = recovery_env
    state = create(status="error")
    state["topic_unreachable_since_ts"] = "2026-08-11T00:00:00+00:00"
    state["topic_recovery_attempts"] = 3
    state["topic_unreachable_event"] = "error"
    continuous.save_state(continuous.state_file_path("recover-me"), state)
    monkeypatch.setattr(config, "TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS", 0)
    platform = _platform(create_result=None)

    async def crash_after_observing_claim(*_args, **_kwargs):
        claimed = continuous.load_state(continuous.state_file_path("recover-me"))
        assert claimed["hq_fallback_sent"] is True
        assert claimed["hq_fallback_outcome"] == "claimed"
        raise TimeoutError("delivery outcome unknown")

    platform.send_message.side_effect = crash_after_observing_claim
    from topic_recovery import recover_unreachable_topic

    result = await recover_unreachable_topic(
        "recover-me",
        platform,
        reason="gone",
        event="error",
        now_fn=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )

    assert result.fallback_sent is False
    fresh = continuous.load_state(continuous.state_file_path("recover-me"))
    assert fresh["hq_fallback_sent"] is True
    assert fresh["hq_fallback_outcome"] == "delivery_uncertain"


@pytest.mark.asyncio
async def test_scheduler_retry_scans_persisted_episode(recovery_env):
    create, continuous, _, _ = recovery_env
    state = create()
    state["topic_unreachable_since_ts"] = datetime.now(timezone.utc).isoformat()
    state["topic_unreachable_reason"] = "gone"
    continuous.save_state(continuous.state_file_path("recover-me"), state)
    platform = _platform(create_result=444)
    from topic_recovery import retry_unreachable_topics

    assert await retry_unreachable_topics(platform, hq_chat_id=-100) == 1
    assert continuous.load_state(
        continuous.state_file_path("recover-me"),
    )["dedicated_thread_id"] == 444
