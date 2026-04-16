"""Helpers for relaying scheduled-task output into visible platform topics."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from ai_backend import AIBackend
from ai_invoke import SILENT_PATTERN, split_message

STATUS_PATTERN = re.compile(r"\[STATUS\s+(.+?)\]")


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
    clean = STATUS_PATTERN.sub("", text or "").strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _error_excerpt(raw_output: str, max_chars: int = 800) -> str:
    lines = [line.strip() for line in (raw_output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    excerpt = "\n".join(lines[-8:])
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt


def _render_result_message(task: dict, parsed_text: str, returncode: int, raw_output: str) -> str:
    title = task.get("description") or task.get("name") or "Scheduled task"
    clean = _clean_result_text(parsed_text)
    if clean:
        return "*%s*\n%s" % (title, clean)

    if returncode == 0:
        return (
            "*%s*\n"
            "_Task completed, but it did not produce any visible output. See logs for details._"
        ) % title

    message = "*%s*\n_Task failed with exit code %d._" % (title, returncode)
    excerpt = _clean_result_text(_error_excerpt(raw_output))
    if excerpt:
        message += "\n\n" + excerpt
    return message


async def deliver_task_output(
    task: dict,
    output_log: Path,
    platform: Any,
    backend: AIBackend,
    returncode: int,
    logger: logging.Logger,
) -> bool:
    """Post the parsed task result into the task's target topic/channel."""
    target_id = _coerce_target_id(task.get("thread_id"))
    if platform is None or target_id is None:
        logger.warning(
            "No delivery target for scheduled task '%s' (thread_id=%r)",
            task.get("name"),
            task.get("thread_id"),
        )
        return False

    raw_output = output_log.read_text(errors="replace") if output_log.exists() else ""
    parsed_response = backend.parse_response(raw_output, returncode)
    parsed_text = _normalize_backend_text(parsed_response)

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
    """Detach a watcher that relays output after the spawned task exits."""
    if platform is None:
        return None

    async def _watch() -> None:
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
            lock_file.unlink(missing_ok=True)

    return asyncio.create_task(_watch())
