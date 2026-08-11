"""Helpers for relaying scheduled-task output into visible platform topics."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from ai_backend import AIBackend
from ai_invoke import SILENT_PATTERN, split_message
from continuous_macro import (
    strip_continuous_macros_for_log,
    strip_control_tokens_for_user,
)
from maintenance import get_maintenance_gate
from runtime_supervisor import get_runtime_supervisor

log = logging.getLogger("robyx.scheduled_delivery")

STATUS_PATTERN = re.compile(r"\[STATUS\s+(.+?)\]")


# ── Spec 006 — structured delivery header ─────────────────────────────────
#
# Every continuous-task delivery prefixes its body with a single header
# line of the form:
#
#   <icon> [<name>] · Step <N>[/<M>] · <state_emoji> <state_label> · HH:MM
#
# Optionally followed by a second line:  → Next: <short description>
#
# Regex below matches the canonical header (used by tests and defensive
# strip-before-prepend logic).

DELIVERY_HEADER_RE = re.compile(
    r"^"
    r"(?P<icon>\S+)\s+"
    r"\[(?P<name>[a-z0-9][a-z0-9-]{0,63})\]\s+·\s+"
    r"Step\s+(?P<step>\d+(?:/\d+)?)\s+·\s+"
    r"(?P<state_emoji>\S+)\s+(?P<state_label>[^·]+?)\s+·\s+"
    r"(?P<hhmm>\d{2}:\d{2})"
    r"$",
)

# State → (emoji, label) mapping per `contracts/delivery-header.md`.
_STATE_PRESENTATION: dict[str, tuple[str, str]] = {
    "pending":         ("▶", "running"),
    "running":         ("▶", "running"),
    "awaiting_input":  ("⏸", "awaiting input"),
    "awaiting-input":  ("⏸", "awaiting input"),  # legacy on-disk value
    "rate_limited":    ("⏳", "rate-limited"),
    "rate-limited":    ("⏳", "rate-limited"),
    "stopped":         ("⏹", "stopped"),
    "paused":          ("⏹", "stopped"),          # legacy
    "completed":       ("✅", "completed"),
    "error":           ("❌", "error"),
    "workspace_closed": ("⚠", "workspace closed"),
    "drain_timeout":   ("⏱", "drain timeout"),
}


# ── Delivery markers (spec 005) ──────────────────────────────────────────────

# Icon per task type. Aliases for one-shot variants collapse to the same glyph
# so callers can read `type` from the queue without pre-normalisation.
TASK_TYPE_ICONS: dict[str, str] = {
    "continuous": "🔄",
    "periodic": "⏰",
    "one-shot": "📌",
    "oneshot": "📌",
    "one_shot": "📌",
    "reminder": "🔔",
}

_MAX_TASK_NAME_CHARS = 64
_MAX_DRAIN_DELETE_BODY_BYTES = 8 * 1024


def format_delivery_message(task_type: str, task_name: str, body: str) -> str:
    """Prefix a scheduled-delivery body with its type icon + task name.

    Contract: ``contracts/delivery-marker.md``. Single chokepoint — agents
    MUST NOT format this themselves. Unknown ``task_type`` yields the body
    unmodified plus a WARN log (spec FR-004 fallback).
    """
    key = (task_type or "").lower().strip()
    icon = TASK_TYPE_ICONS.get(key)
    safe_body = body or ""
    if icon is None:
        log.warning(
            "format_delivery_message: unknown task_type=%r (name=%r) — "
            "delivering without marker",
            task_type, task_name,
        )
        return safe_body

    name = (task_name or "").strip() or "?"
    if len(name) > _MAX_TASK_NAME_CHARS:
        name = name[: _MAX_TASK_NAME_CHARS - 1] + "…"

    if not safe_body.strip():
        return "%s [%s]" % (icon, name)
    return "%s [%s] %s" % (icon, name, safe_body)


def _normalize_backend_text(parsed_response: Any) -> str:
    if isinstance(parsed_response, dict):
        return (parsed_response.get("text", "") or "").strip()
    return (parsed_response or "").strip()


def _coerce_target_id(raw_target: Any) -> Any:
    if raw_target is None:
        return None
    if isinstance(raw_target, str):
        target = raw_target.strip()
        if target in ("", "-"):
            return None
        if target.isdigit():
            return int(target)
        return target
    return raw_target


def _message_id_from_ref(ref: Any) -> Any:
    if isinstance(ref, dict):
        return ref.get("message_id") or ref.get("ts") or ref.get("id")
    return getattr(ref, "message_id", None) or getattr(ref, "id", None)


def _clean_result_text(text: str) -> str:
    # Scrub any stray continuous-task macro tokens and [STATUS …] tokens.
    # Scheduled subprocess output has no interactive agent context, so we
    # MUST NOT dispatch a new continuous task from here — but we MUST still
    # strip the tokens so a leaked macro cannot reach the chat (spec 004
    # FR-001/FR-011; spec 005 T008 consolidates to the canonical helper).
    # We still log stray-token counts via the legacy wrapper for WARN-level
    # observability on the scheduled path.
    strip_continuous_macros_for_log(text or "")
    return strip_control_tokens_for_user(text or "")


def _bounded_drain_delete_body(parsed_text: str) -> tuple[str, bool]:
    """Return only chat-safe output, bounded to the FR-021 8 KiB limit.

    Raw CLI output and stderr excerpts are intentionally excluded: the
    journal receives exactly the normalised agent body that could otherwise
    have gone to chat, with all control tokens and agent-authored delivery
    headers removed.
    """
    body = _strip_agent_header(_clean_result_text(parsed_text))
    body = SILENT_PATTERN.sub("", body).strip()
    encoded = body.encode("utf-8")
    if len(encoded) <= _MAX_DRAIN_DELETE_BODY_BYTES:
        return body, False
    bounded = encoded[:_MAX_DRAIN_DELETE_BODY_BYTES].decode(
        "utf-8", errors="ignore",
    )
    return bounded, True


def _error_excerpt(raw_output: str, max_chars: int = 800) -> str:
    lines = [line.strip() for line in (raw_output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    excerpt = "\n".join(lines[-8:])
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt


def _format_step_counter(current_num: int | None, total: int | None) -> str:
    if current_num is None:
        return "0"
    if total is not None and total > 0:
        return "%d/%d" % (current_num, total)
    return str(current_num)


def _state_presentation(status: str, override: str | None = None) -> tuple[str, str]:
    """Return the (emoji, label) tuple for the given status.

    ``override`` lets callers force a specific presentation regardless of
    the on-state status value — used for special cases like
    ``workspace_closed`` and ``drain_timeout`` that are event-bound
    rather than persistent-state-bound.
    """
    key = override or status or "running"
    return _STATE_PRESENTATION.get(key, ("▶", "running"))


def _read_continuous_state(task_name: str) -> dict | None:
    """Load a continuous task's state if the queue entry provides its name.

    Resolves through ``continuous.state_file_path`` / ``load_state`` so
    the test-suite monkeypatches of ``CONTINUOUS_DIR`` still apply.
    Returns None if the state file is missing or the task is not
    continuous — the caller uses defaults in that case.
    """
    if not task_name:
        return None
    try:
        from continuous import load_state, state_file_path
        return load_state(state_file_path(task_name))
    except Exception:
        return None


def _build_continuous_header(
    task_name: str,
    state: dict | None,
    state_override: str | None = None,
    hhmm: str | None = None,
) -> tuple[str, str | None]:
    """Compute the header line + optional ``→ Next:`` line for a
    continuous-task delivery.

    ``state_override`` forces the state-label (e.g. ``workspace_closed``
    for a drain delivery, regardless of the stored status).

    Returns ``(header, next_line or None)``.
    """
    from datetime import datetime

    # Step counter
    step_num: int | None = None
    total_steps: int | None = None
    next_desc: str | None = None
    status_value = "running"
    if state:
        current_step = state.get("current_step") or {}
        step_num = current_step.get("number")
        if step_num is None:
            step_num = state.get("total_steps_completed") or 0
        program = state.get("program") or {}
        if isinstance(program.get("total_steps"), int):
            total_steps = program["total_steps"]
        next_step = state.get("next_step") or {}
        next_desc = next_step.get("description")
        status_value = state.get("status") or "running"

    # Resolve presentation
    emoji, label = _state_presentation(status_value, override=state_override)
    # For rate-limited, append HH:MM of recovery time (best-effort).
    if state_override is None and status_value in ("rate_limited", "rate-limited"):
        until_iso = (state or {}).get("rate_limited_until")
        if until_iso:
            try:
                dt = datetime.fromisoformat(until_iso)
                label = "rate-limited until %s" % dt.strftime("%H:%M")
            except ValueError:
                pass

    now_hhmm = hhmm or datetime.now().strftime("%H:%M")
    step_str = _format_step_counter(step_num, total_steps)
    header = "🔄 [%s] · Step %s · %s %s · %s" % (
        task_name or "?",
        step_str,
        emoji,
        label,
        now_hhmm,
    )

    next_line: str | None = None
    if next_desc and state_override not in ("completed", "error", "workspace_closed"):
        trimmed = next_desc.strip()
        if len(trimmed) > 80:
            trimmed = trimmed[:79] + "…"
        next_line = "→ Next: %s" % trimmed

    return header, next_line


def _strip_agent_header(body: str) -> str:
    """Remove any first-line header that matches :data:`DELIVERY_HEADER_RE`.

    Defensive: if an agent's output starts with something that looks like
    a canonical header, the renderer discards it before prepending the
    authoritative one. This prevents double-headers when agents drift
    from their prompts.
    """
    if not body:
        return body
    lines = body.splitlines()
    if lines and DELIVERY_HEADER_RE.match(lines[0].strip()):
        # Drop the header line and any immediate blank line separator.
        idx = 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        return "\n".join(lines[idx:])
    return body


def _render_result_message(
    task: dict,
    parsed_text: str,
    returncode: int,
    raw_output: str,
    state_override: str | None = None,
) -> str:
    title = task.get("description") or task.get("name") or "Scheduled task"
    task_type = task.get("type") or "continuous"
    task_name = task.get("name") or title
    clean = _clean_result_text(parsed_text)
    clean = _strip_agent_header(clean)

    if clean:
        body = clean
    elif returncode == 0:
        body = (
            "_Task completed, but it did not produce any visible output. "
            "See logs for details._"
        )
    else:
        body = "_Task failed with exit code %d._" % returncode
        excerpt = _clean_result_text(_error_excerpt(raw_output))
        if excerpt:
            body += "\n\n" + excerpt

    # Spec 006: continuous tasks get a rich structured header via the
    # delivery chokepoint. Non-continuous types keep the existing
    # icon+name format (unchanged).
    if task_type == "continuous":
        state = _read_continuous_state(task_name)
        header, next_line = _build_continuous_header(
            task_name, state, state_override=state_override,
        )
        parts = [header]
        if next_line:
            parts.append(next_line)
        parts.append("")  # blank separator
        parts.append(body)
        return "\n".join(parts)

    # Non-continuous — legacy single-line icon prefix (spec 005 contract).
    return format_delivery_message(task_type, task_name, body)


async def deliver_task_output(
    task: dict,
    output_log: Path,
    platform: Any,
    backend: AIBackend,
    returncode: int,
    logger: logging.Logger,
) -> bool:
    """Post the parsed task result into the task's target topic/channel.

    Spec 006: for continuous tasks, delivery prefers ``dedicated_thread_id``
    (stored in the task's state.json) over the queue entry's ``thread_id``.
    Falls back to the queue entry's thread_id when no dedicated topic is
    set (pre-migration snapshots, platforms without topic primitives).
    Emits a ``step_complete`` journal event even for ``[SILENT]`` steps
    so pull-based queries reconstruct full history (FR-005).
    """
    task_name = task.get("name") or ""
    task_type = task.get("type") or "continuous"
    is_continuous = task_type == "continuous"

    # Spec 006 — resolve the dedicated topic id, if present.
    target_id: Any = None
    state: dict | None = None
    if is_continuous and task_name:
        try:
            from continuous import load_state, state_file_path
            state = load_state(state_file_path(task_name))
            if state and state.get("dedicated_thread_id"):
                target_id = state["dedicated_thread_id"]
        except Exception:
            pass
    if target_id is None:
        target_id = _coerce_target_id(task.get("thread_id"))

    raw_output = output_log.read_text(errors="replace") if output_log.exists() else ""
    parsed_response = backend.parse_response(raw_output, returncode)
    parsed_text = _normalize_backend_text(parsed_response)

    # FR-021/T061: deletion may win while a workspace-close drain watcher is
    # reconciling the final child output. Once delete is in progress the body
    # must never race topic archival. Persist the bounded chat-safe body and
    # let the lifecycle path post one final journal reference before archive.
    delete_in_progress = False
    archive_completed = False
    if is_continuous and task_name:
        fresh_state = _read_continuous_state(task_name)
        if fresh_state is not None:
            state = fresh_state
            delete_in_progress = bool(state.get("delete_in_progress_at"))
            archive_completed = (
                state.get("status") == "deleted"
                and bool(state.get("archived_at"))
            )
    if is_continuous and task_name and (delete_in_progress or archive_completed):
        body, body_truncated = _bounded_drain_delete_body(parsed_text)
        try:
            import events as events_mod
            events_mod.append(
                task_name=task_name,
                task_type="continuous",
                event_type="step_complete",
                outcome="archived" if archive_completed else "delete_in_progress",
                payload={
                    "delivery": "drain_during_delete",
                    "returncode": returncode,
                    "body": body,
                    "body_truncated": body_truncated,
                },
            )
        except Exception:
            logger.warning(
                "Could not journal delete-drain output for '%s'",
                task_name,
                exc_info=True,
            )
        if not archive_completed and state is not None:
            try:
                from datetime import datetime, timezone
                from continuous import save_state, state_file_path
                state["drain_delete_output_recorded_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                state["drain_delete_reference_pending"] = True
                save_state(state_file_path(task_name), state)
            except Exception:
                logger.warning(
                    "Could not persist delete-drain reference for '%s'",
                    task_name,
                    exc_info=True,
                )
        return True

    # Spec 006 — journal step_complete for continuous tasks regardless of
    # whether the delivery itself is SILENT. This keeps [GET_EVENTS]
    # queries complete (FR-005 silent-step convention).
    if is_continuous and task_name:
        try:
            import events as events_mod
            outcome = "ok" if returncode == 0 else "failed"
            events_mod.append(
                task_name=task_name,
                task_type="continuous",
                event_type="step_complete",
                outcome=outcome,
                payload={"returncode": returncode},
            )
        except Exception:
            pass

    if SILENT_PATTERN.search(parsed_text):
        residual = SILENT_PATTERN.sub("", parsed_text)
        residual = STATUS_PATTERN.sub("", residual).strip()
        if not residual:
            if returncode == 0:
                logger.info(
                    "Scheduled task '%s' emitted [SILENT] — suppressing delivery",
                    task.get("name"),
                )
                return True
            # Failure case: never silent — fall through with empty parsed
            # text so _render_result_message produces the error message.
            parsed_text = ""
        else:
            parsed_text = residual

    if platform is None or target_id is None:
        logger.warning(
            "No delivery target for scheduled task '%s' (thread_id=%r)",
            task.get("name"),
            task.get("thread_id"),
        )
        return False

    state_override = (
        state.get("delivery_state_override")
        if state is not None
        else None
    )
    message = _render_result_message(
        task,
        parsed_text,
        returncode,
        raw_output,
        state_override=state_override,
    )

    max_len = getattr(platform, "max_message_length", 4000)
    last_message_ref: Any = None
    topic_operation_failed = False
    for chunk in split_message(message, max_len=max_len):
        failure_reason = "delivery returned false"
        try:
            ref_method = getattr(type(platform), "send_to_channel_with_ref", None)
            if ref_method is not None:
                last_message_ref = await platform.send_to_channel_with_ref(
                    target_id,
                    chunk,
                    parse_mode="Markdown",
                )
                sent = bool(last_message_ref)
            else:
                sent = await platform.send_to_channel(
                    target_id,
                    chunk,
                    parse_mode="Markdown",
                )
            if not sent:
                if ref_method is not None:
                    last_message_ref = await platform.send_to_channel_with_ref(
                        target_id,
                        chunk,
                        parse_mode="",
                    )
                    sent = bool(last_message_ref)
                else:
                    sent = await platform.send_to_channel(target_id, chunk, parse_mode="")
        except Exception as exc:
            from messaging.base import TopicUnreachable
            if not isinstance(exc, TopicUnreachable):
                raise
            sent = False
            failure_reason = exc.reason or str(exc)
        if not sent:
            if is_continuous and task_name:
                event = None
                if returncode != 0:
                    event = "error"
                elif state and state.get("status") in ("awaiting_input", "awaiting-input"):
                    event = "awaiting_input"
                from topic_recovery import recover_unreachable_topic
                recovery = await recover_unreachable_topic(
                    task_name,
                    platform,
                    reason=failure_reason,
                    event=event,
                    pending_delivery=chunk,
                )
                if recovery.pending_delivered:
                    target_id = recovery.recreated_thread_id
                    continue
            logger.error(
                "Failed to deliver scheduled task '%s' result to target %r",
                task.get("name"),
                target_id,
            )
            return False
    if is_continuous and task_name:
        if state and state.get("status") in ("awaiting_input", "awaiting-input"):
            message_id = _message_id_from_ref(last_message_ref)
            if message_id is not None:
                try:
                    from config import CHAT_ID
                    from continuous import (
                        load_state,
                        pin_awaiting_message,
                        save_state,
                        state_file_path,
                        update_topic_state_marker,
                    )
                    fresh = load_state(state_file_path(task_name)) or state
                    pinned = await pin_awaiting_message(
                        fresh,
                        platform,
                        CHAT_ID,
                        message_id,
                    )
                    marker_updated = await update_topic_state_marker(fresh, platform)
                    if pinned and marker_updated:
                        fresh["topic_marker_status"] = fresh.get("status")
                        save_state(state_file_path(task_name), fresh)
                    if not pinned or not marker_updated:
                        from topic_recovery import recover_unreachable_topic
                        recovery = await recover_unreachable_topic(
                            task_name,
                            platform,
                            reason="awaiting pin or marker returned false",
                            event="awaiting_input",
                            pending_delivery=chunk,
                        )
                        topic_operation_failed = not recovery.pending_delivered
                except Exception as exc:
                    from messaging.base import TopicUnreachable
                    if isinstance(exc, TopicUnreachable):
                        from topic_recovery import recover_unreachable_topic
                        recovery = await recover_unreachable_topic(
                            task_name,
                            platform,
                            reason=exc.reason or str(exc),
                            event="awaiting_input",
                            pending_delivery=chunk,
                        )
                        topic_operation_failed = not recovery.pending_delivered
                    else:
                        logger.warning(
                            "Awaiting-input pin/marker failed for '%s'",
                            task_name,
                            exc_info=True,
                        )
        if not topic_operation_failed:
            from topic_recovery import mark_topic_reachable
            await mark_topic_reachable(task_name)
    if state_override and state is not None:
        try:
            from continuous import load_state, save_state, state_file_path
            state_path = state_file_path(task_name)
            fresh = load_state(state_path)
            if fresh and fresh.get("id") == state.get("id"):
                fresh.pop("delivery_state_override", None)
                save_state(state_path, fresh)
        except Exception:
            logger.warning(
                "Could not clear delivery override for '%s'",
                task_name,
                exc_info=True,
            )
    return True


def start_task_delivery_watch(
    task: dict,
    proc: asyncio.subprocess.Process,
    output_log: Path,
    lock_file: Path,
    platform: Any,
    backend: AIBackend,
    logger: logging.Logger,
) -> asyncio.Task:
    """Start a supervised watcher that relays output after child exit.

    Spec 006 US4: also spawns a heartbeat refresher that rewrites the
    lock file's timestamp every ``LOCK_HEARTBEAT_INTERVAL_SECONDS`` while
    the subprocess is alive. If the subprocess dies without clean
    teardown (SIGKILL, OOM, host crash), the heartbeat goes stale within
    ``LOCK_STALE_THRESHOLD_SECONDS`` and the scheduler reclaims on its
    next cycle (FR-019/FR-020).
    """
    import asyncio as _asyncio
    from scheduler import refresh_heartbeat

    try:
        from config import LOCK_HEARTBEAT_INTERVAL_SECONDS as _interval
    except Exception:
        _interval = 30

    heartbeat_cancelled = _asyncio.Event()

    async def _heartbeat_loop() -> None:
        while not heartbeat_cancelled.is_set():
            try:
                refresh_heartbeat(lock_file, proc.pid)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "heartbeat refresh failed for '%s': %s",
                    task.get("name"), exc,
                )
            try:
                await _asyncio.wait_for(
                    heartbeat_cancelled.wait(),
                    timeout=_interval,
                )
            except _asyncio.TimeoutError:
                continue

    async def _watch() -> None:
        heartbeat_task = _asyncio.create_task(_heartbeat_loop())
        returncode = 1
        try:
            returncode = await proc.wait()
            if not isinstance(getattr(proc, "returncode", None), int):
                try:
                    proc.returncode = returncode
                except (AttributeError, TypeError):
                    pass
            # The direct CLI can exit while a worker it spawned keeps the
            # isolated group alive. Reap that remainder before treating output
            # as final; otherwise a delivered task can still mutate files.
            if supervisor.process_tree_alive(proc):
                stopped = await supervisor.terminate_process(
                    proc,
                    grace_seconds=2.0,
                )
                if not stopped:
                    raise RuntimeError("scheduled process tree did not stop")
            if platform is not None:
                await deliver_task_output(
                    task,
                    output_log,
                    platform,
                    backend,
                    returncode,
                    logger,
                )
        except Exception as exc:
            logger.error(
                "Scheduled-task delivery watcher crashed for '%s': %s",
                task.get("name"),
                exc,
                exc_info=True,
            )
        finally:
            heartbeat_cancelled.set()
            try:
                await _asyncio.wait_for(heartbeat_task, timeout=2.0)
            except _asyncio.CancelledError:
                heartbeat_task.cancel()
                await _asyncio.gather(heartbeat_task, return_exceptions=True)
                raise
            except Exception:
                heartbeat_task.cancel()
                await _asyncio.gather(heartbeat_task, return_exceptions=True)
            finally:
                if (
                    getattr(proc, "returncode", None) is not None
                    and not supervisor.process_tree_alive(proc)
                ):
                    lock_file.unlink(missing_ok=True)
                    supervisor.untrack_process(proc)
                else:
                    logger.error(
                        "Keeping lock and orphan PID for unreaped scheduled task '%s'",
                        task.get("name"),
                    )

    supervisor = get_runtime_supervisor()
    supervisor.track_process(
        proc,
        owner="scheduled:%s" % (task.get("name") or "?"),
        process_group=True,
    )
    gate = get_maintenance_gate()
    watch_coroutine = _watch()
    try:
        handoff = gate.handoff_shared()
    except RuntimeError:
        handoff = None
        leased_coroutine = _watch_with_new_maintenance_lease(
            watch_coroutine,
            gate,
        )
    else:
        leased_coroutine = _watch_with_maintenance_lease(watch_coroutine, handoff)
    try:
        return supervisor.spawn(
            leased_coroutine,
            name="scheduled_delivery:%s:%s" % (task.get("name") or "?", proc.pid),
            key="scheduled_delivery:%s" % proc.pid,
        )
    except BaseException:
        if handoff is not None:
            handoff.cancel()
        leased_coroutine.close()
        watch_coroutine.close()
        raise


async def _watch_with_maintenance_lease(coroutine, handoff) -> None:
    """Retain the scheduler's shared lease through delivery reconciliation."""
    async with handoff:
        await coroutine


async def _watch_with_new_maintenance_lease(coroutine, gate) -> None:
    """Compatibility path for callers not dispatched by a scheduler cycle."""
    async with gate.shared():
        await coroutine
