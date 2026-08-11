"""Persistent dedicated-topic recovery and FR-002a HQ fallback.

All mutations for one task run under the same lifecycle lock used by
stop/complete/delete/create.  This prevents a late recreation from reviving a
deleted generation and makes the HQ suppression flag an exactly-once claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("robyx.topic_recovery")

ACTIONABLE_EVENTS = frozenset({"awaiting_input", "error", "task_death"})
MAX_RECOVERY_ATTEMPTS = 3


@dataclass(frozen=True)
class RecoveryResult:
    recreated_thread_id: Any = None
    pending_delivered: bool = False
    fallback_sent: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback


def _safe_reason(reason: str) -> str:
    # Platform exceptions can include large response bodies.  Persist only a
    # compact diagnostic and never include task output in the HQ fallback.
    clean = " ".join(str(reason or "dedicated topic delivery failed").split())
    return clean[:240]


def _actionable_event(state: dict, explicit: str | None) -> str | None:
    if explicit in ACTIONABLE_EVENTS:
        return explicit
    status = state.get("status")
    if status in ("awaiting_input", "awaiting-input"):
        return "awaiting_input"
    if status == "error":
        return "error"
    return None


async def _send_pending(platform: Any, thread_id: Any, pending: str) -> tuple[bool, Any]:
    """Replay one pending delivery, preserving an exact message reference."""
    ref_method = getattr(type(platform), "send_to_channel_with_ref", None)
    message_ref = None
    for parse_mode in ("Markdown", ""):
        try:
            if ref_method is not None:
                message_ref = await platform.send_to_channel_with_ref(
                    thread_id,
                    pending,
                    parse_mode=parse_mode,
                )
                delivered = bool(message_ref)
            else:
                delivered = bool(
                    await platform.send_to_channel(
                        thread_id,
                        pending,
                        parse_mode=parse_mode,
                    )
                )
            if delivered:
                return True, message_ref
        except Exception as exc:
            log.warning("Pending delivery to topic %s failed: %s", thread_id, exc)
            return False, None
    return False, message_ref


async def _restore_awaiting_visuals(
    state: dict,
    platform: Any,
    message_ref: Any,
    chat_id: Any,
) -> None:
    """Pin a recovered awaiting message and restore its state marker."""
    if message_ref is None:
        return
    from continuous import pin_awaiting_message, update_topic_state_marker

    message_id = (
        message_ref.get("message_id")
        or message_ref.get("ts")
        or message_ref.get("id")
        if isinstance(message_ref, dict)
        else getattr(message_ref, "message_id", None)
        or getattr(message_ref, "id", None)
    )
    if message_id is None:
        return
    await pin_awaiting_message(state, platform, chat_id, message_id)
    await update_topic_state_marker(state, platform)
    state["topic_marker_status"] = state.get("status")


async def _compensate_created_topic(
    platform: Any,
    thread_id: Any,
    display_name: str,
) -> bool:
    """Best-effort cleanup for a topic created by a failed transaction."""
    try:
        archive = getattr(platform, "archive_topic", None)
        if archive is not None and await archive(thread_id, display_name):
            return True
    except Exception:
        log.warning("Could not archive compensating topic %s", thread_id, exc_info=True)
    try:
        close = getattr(platform, "close_channel", None)
        return bool(close is not None and await close(thread_id))
    except Exception:
        log.error("Could not close compensating topic %s", thread_id, exc_info=True)
        return False


async def recover_unreachable_topic(
    task_name: str,
    platform: Any,
    *,
    reason: str,
    event: str | None = None,
    pending_delivery: str | None = None,
    hq_chat_id: Any = None,
    now_fn: Callable[[], datetime] = _utc_now,
) -> RecoveryResult:
    """Persist a failure, recreate/repoint, or emit one actionable fallback.

    A failed create consumes one of three persisted attempts.  Subsequent
    scheduler cycles continue the episode without resetting its first-failure
    timestamp.  Routine events are never promoted to HQ.
    """
    if platform is None:
        return RecoveryResult()

    from config import CHAT_ID, TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS
    from continuous import load_state, save_state, state_file_path
    from lifecycle_macros import _lifecycle_task_lock
    from scheduler import repoint_continuous_topic

    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    state_path = state_file_path(task_name)

    async with _lifecycle_task_lock(task_name):
        state = load_state(state_path)
        if state is None or state.get("status") == "deleted":
            return RecoveryResult()

        if not state.get("topic_unreachable_since_ts"):
            state["topic_unreachable_since_ts"] = now.isoformat()
            state["topic_recovery_attempts"] = 0
        state["topic_unreachable_reason"] = _safe_reason(reason)
        actionable = _actionable_event(state, event)
        if actionable is not None:
            state["topic_unreachable_event"] = actionable
        if pending_delivery:
            state["topic_pending_delivery"] = pending_delivery
        save_state(state_path, state)

        pending = state.get("topic_pending_delivery")
        candidate = state.get("topic_recovery_candidate_thread_id")
        candidate_retry_failed = False
        if (
            pending is not None
            and candidate is not None
            and candidate == state.get("dedicated_thread_id")
        ):
            delivered, message_ref = await _send_pending(platform, candidate, pending)
            if delivered:
                state["topic_pending_delivery"] = None
                state["topic_recovery_candidate_thread_id"] = None
                state["topic_unreachable_since_ts"] = None
                state["topic_unreachable_reason"] = None
                state["topic_unreachable_event"] = None
                state["topic_recovery_attempts"] = 0
                state["hq_fallback_sent"] = False
                state["hq_fallback_outcome"] = None
                state["hq_fallback_claimed_at"] = None
                if actionable == "awaiting_input":
                    await _restore_awaiting_visuals(
                        state,
                        platform,
                        message_ref,
                        CHAT_ID if hq_chat_id is None else hq_chat_id,
                    )
                save_state(state_path, state)
                return RecoveryResult(candidate, True, False)
            state["topic_unreachable_reason"] = "recreated topic delivery failed"
            save_state(state_path, state)
            candidate_retry_failed = True

        attempts = int(state.get("topic_recovery_attempts") or 0)
        new_thread_id = None
        if (
            not candidate_retry_failed
            and attempts < MAX_RECOVERY_ATTEMPTS
            and hasattr(platform, "create_channel")
        ):
            state["topic_recovery_attempts"] = attempts + 1
            save_state(state_path, state)
            try:
                new_thread_id = await platform.create_channel(
                    "[Continuous] %s" % (state.get("name") or task_name),
                )
            except Exception as exc:
                state["topic_unreachable_reason"] = _safe_reason(str(exc))
                save_state(state_path, state)
                new_thread_id = None

        if new_thread_id is not None:
            original = dict(state)
            state["dedicated_thread_id"] = new_thread_id
            state["topic_unreachable_since_ts"] = None
            state["topic_unreachable_reason"] = None
            state["topic_unreachable_event"] = None
            state["topic_recovery_attempts"] = 0
            state["hq_fallback_sent"] = False
            state["hq_fallback_outcome"] = None
            state["hq_fallback_claimed_at"] = None
            state["topic_recovery_candidate_thread_id"] = (
                new_thread_id if state.get("topic_pending_delivery") is not None else None
            )
            try:
                # State and queue are individually atomic.  They are written
                # synchronously inside the lifecycle transaction; a queue
                # failure compensates by restoring the prior state snapshot.
                save_state(state_path, state)
                repoint_continuous_topic(task_name, new_thread_id)
            except Exception:
                save_state(state_path, original)
                await _compensate_created_topic(
                    platform,
                    new_thread_id,
                    state.get("name") or task_name,
                )
                raise

            try:
                import events
                events.append(
                    task_name=task_name,
                    task_type="continuous",
                    event_type="topic_recreated",
                    outcome="recovered",
                    payload={
                        "old_thread_id": original.get("dedicated_thread_id"),
                        "new_thread_id": new_thread_id,
                    },
                )
            except Exception:
                log.warning("Could not journal topic recreation for %s", task_name)

            pending = state.get("topic_pending_delivery")
            delivered = pending is None
            if pending is not None:
                delivered, message_ref = await _send_pending(
                    platform,
                    new_thread_id,
                    pending,
                )
                if delivered:
                    state = load_state(state_path) or state
                    state["topic_pending_delivery"] = None
                    state["topic_recovery_candidate_thread_id"] = None
                    if actionable == "awaiting_input" and message_ref is not None:
                        await _restore_awaiting_visuals(
                            state,
                            platform,
                            message_ref,
                            CHAT_ID if hq_chat_id is None else hq_chat_id,
                        )
                    save_state(state_path, state)
                else:
                    # Creation alone is not proof of reachability when the
                    # first send fails.  Preserve the episode for retry.
                    state = load_state(state_path) or state
                    state["topic_unreachable_since_ts"] = now.isoformat()
                    state["topic_unreachable_reason"] = "recreated topic delivery failed"
                    save_state(state_path, state)
            return RecoveryResult(new_thread_id, delivered, False)

        first_failure = _parse_ts(state.get("topic_unreachable_since_ts"), now)
        elapsed = max(0.0, (now - first_failure).total_seconds())
        pending_event = state.get("topic_unreachable_event")
        if (
            pending_event not in ACTIONABLE_EVENTS
            or elapsed < TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS
            or state.get("hq_fallback_sent")
        ):
            return RecoveryResult()

        control_room = getattr(platform, "control_room_id", None)
        destination = CHAT_ID if hq_chat_id is None else hq_chat_id
        body = (
            "⚠ Continuous task `%s` needs attention: `%s`. Its dedicated "
            "topic is unreachable (%s). Recreate the topic or inspect "
            "`[GET_EVENTS task=\"%s\"]`."
            % (
                task_name,
                pending_event,
                state.get("topic_unreachable_reason") or "unknown reason",
                task_name,
            )
        )
        # Claim before the external side effect.  A crash after the send but
        # before persistence would otherwise duplicate the HQ alert on boot.
        # An exception/None outcome is delivery-uncertain and remains
        # suppressed; only explicit ``False`` is treated as certain non-send.
        state = load_state(state_path) or state
        state["hq_fallback_sent"] = True
        state["hq_fallback_outcome"] = "claimed"
        state["hq_fallback_claimed_at"] = now.isoformat()
        save_state(state_path, state)
        try:
            sent_ref = await platform.send_message(
                destination,
                body,
                thread_id=control_room,
                parse_mode="Markdown",
            )
        except Exception as exc:
            log.error("HQ fallback failed for %s: %s", task_name, exc)
            state = load_state(state_path) or state
            state["hq_fallback_outcome"] = "delivery_uncertain"
            save_state(state_path, state)
            return RecoveryResult()
        if sent_ref is False:
            state = load_state(state_path) or state
            state["hq_fallback_sent"] = False
            state["hq_fallback_outcome"] = "not_delivered"
            save_state(state_path, state)
            return RecoveryResult()
        if sent_ref is None:
            state = load_state(state_path) or state
            state["hq_fallback_outcome"] = "delivery_uncertain"
            save_state(state_path, state)
            return RecoveryResult()

        state = load_state(state_path) or state
        state["hq_fallback_outcome"] = "posted"
        save_state(state_path, state)
        try:
            import events
            events.append(
                task_name=task_name,
                task_type="continuous",
                event_type="hq_fallback_sent",
                outcome="posted",
                payload={"event": pending_event},
            )
        except Exception:
            log.warning("Could not journal HQ fallback for %s", task_name)
        return RecoveryResult(fallback_sent=True)


async def retry_unreachable_topics(
    platform: Any,
    *,
    hq_chat_id: Any = None,
    now_fn: Callable[[], datetime] = _utc_now,
) -> int:
    """Retry every persisted recovery episode once during a scheduler tick."""
    if platform is None:
        return 0
    from continuous import CONTINUOUS_DIR, load_state

    recovered = 0
    if not CONTINUOUS_DIR.exists():
        return recovered
    for task_dir in sorted(CONTINUOUS_DIR.iterdir()):
        state_path = task_dir / "state.json"
        if not state_path.exists():
            continue
        state = load_state(state_path)
        if not state or not state.get("topic_unreachable_since_ts"):
            continue
        result = await recover_unreachable_topic(
            state.get("name") or task_dir.name,
            platform,
            reason=state.get("topic_unreachable_reason") or "delivery failed",
            event=state.get("topic_unreachable_event"),
            hq_chat_id=hq_chat_id,
            now_fn=now_fn,
        )
        if result.recreated_thread_id is not None:
            recovered += 1
    return recovered


async def mark_topic_reachable(task_name: str) -> None:
    """Clear one recovery episode after a successful delivery."""
    from continuous import load_state, save_state, state_file_path
    from lifecycle_macros import _lifecycle_task_lock

    state_path = state_file_path(task_name)
    observed = load_state(state_path)
    if observed is None or not (
        observed.get("topic_unreachable_since_ts")
        or observed.get("hq_fallback_sent")
        or observed.get("topic_pending_delivery")
    ):
        return
    async with _lifecycle_task_lock(task_name):
        state = load_state(state_path)
        if state is None or not (
            state.get("topic_unreachable_since_ts")
            or state.get("hq_fallback_sent")
            or state.get("topic_pending_delivery")
        ):
            return
        state["topic_unreachable_since_ts"] = None
        state["topic_unreachable_reason"] = None
        state["topic_unreachable_event"] = None
        state["topic_recovery_attempts"] = 0
        state["topic_pending_delivery"] = None
        state["topic_recovery_candidate_thread_id"] = None
        state["hq_fallback_sent"] = False
        state["hq_fallback_outcome"] = None
        state["hq_fallback_claimed_at"] = None
        save_state(state_path, state)


__all__ = [
    "ACTIONABLE_EVENTS",
    "RecoveryResult",
    "mark_topic_reachable",
    "recover_unreachable_topic",
    "retry_unreachable_topics",
]
