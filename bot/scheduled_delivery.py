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

    message = _render_result_message(task, parsed_text, returncode, raw_output)

    max_len = getattr(platform, "max_message_length", 4000)
    for chunk in split_message(message, max_len=max_len):
        sent = await platform.send_to_channel(target_id, chunk, parse_mode="Markdown")
        if not sent:
            sent = await platform.send_to_channel(target_id, chunk, parse_mode="")
        if not sent:
            logger.error(
                "Failed to deliver scheduled task '%s' result to target %r",
                task.get("name"),
                target_id,
            )
            return False
    return True


def start_task_delivery_watch(
    task: dict,
    proc: asyncio.subprocess.Process,
    output_log: Path,
    lock_file: Path,
    platform: Any,
    backend: AIBackend,
    logger: logging.Logger,
) -> asyncio.Task | None:
    """Detach a watcher that relays output after the spawned task exits.

    Spec 006 US4: also spawns a heartbeat refresher that rewrites the
    lock file's timestamp every ``LOCK_HEARTBEAT_INTERVAL_SECONDS`` while
    the subprocess is alive. If the subprocess dies without clean
    teardown (SIGKILL, OOM, host crash), the heartbeat goes stale within
    ``LOCK_STALE_THRESHOLD_SECONDS`` and the scheduler reclaims on its
    next cycle (FR-019/FR-020).
    """
    if platform is None:
        return None

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
            except (_asyncio.TimeoutError, Exception):
                heartbeat_task.cancel()
            lock_file.unlink(missing_ok=True)

    return asyncio.create_task(_watch())
