"""Robyx — Reminder Engine.

Pure-Python scheduler that fires messages at exact times via the Platform
abstraction. No LLM involved — reads reminders.json directly.

Features:
- Runs every minute via APScheduler
- Late-firing: if service was down, past-due reminders fire immediately on restart
- Idempotent: fired reminders are marked 'sent' and never re-fired
- Thread-safe: file I/O protected by a lock
"""

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import CLAIM_TIMEOUT_SECONDS, MAX_REMINDER_ATTEMPTS

SEND_TIMEOUT_SECONDS = 30

logger = logging.getLogger("robyx.reminders")

_lock = threading.Lock()


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed to load reminders.json: %s", e)
        return []


def _save(path: Path, reminders: list[dict]) -> None:
    """Atomically rewrite the reminders file (write-temp + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(reminders, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _clear_claim(reminder: dict) -> None:
    reminder.pop("claim_token", None)
    reminder.pop("claimed_at", None)


def _reset_stale_claims(reminders: list[dict], now: datetime) -> bool:
    changed = False
    for reminder in reminders:
        if reminder.get("status") != "sending":
            continue

        claimed_at_raw = reminder.get("claimed_at")
        try:
            claimed_at = _parse_timestamp(claimed_at_raw) if claimed_at_raw else None
        except ValueError:
            claimed_at = None

        if claimed_at is None or (now - claimed_at).total_seconds() > CLAIM_TIMEOUT_SECONDS:
            logger.warning(
                "Resetting stale reminder claim %s back to pending",
                reminder.get("id"),
            )
            reminder["status"] = "pending"
            _clear_claim(reminder)
            changed = True

    return changed


def _claim_due_reminders(
    reminders_file: Path,
    default_chat_id: Any = None,
) -> list[dict]:
    """Reserve due reminders under the file lock, then return the work list.

    The returned entries are safe to send outside the lock because the file
    already carries a durable claim token for each reserved reminder.
    """
    with _lock:
        reminders = _load(reminders_file)
        if not reminders:
            return []

        now = datetime.now(timezone.utc)
        changed = _reset_stale_claims(reminders, now)
        due: list[dict] = []

        for reminder in reminders:
            if reminder.get("status") != "pending":
                continue

            try:
                fire_at = _parse_timestamp(reminder["fire_at"])
            except (KeyError, ValueError) as e:
                logger.warning(
                    "Invalid fire_at for reminder %s: %s",
                    reminder.get("id"),
                    e,
                )
                reminder["status"] = "invalid"
                changed = True
                continue

            if fire_at > now:
                continue

            attempts = reminder.get("attempts", 0)
            if attempts >= MAX_REMINDER_ATTEMPTS:
                logger.warning(
                    "Reminder %s exceeded max attempts (%d), marking failed",
                    reminder.get("id"),
                    MAX_REMINDER_ATTEMPTS,
                )
                reminder["status"] = "failed"
                changed = True
                continue

            chat_id = reminder.get("chat_id", default_chat_id)
            thread_id = reminder.get("thread_id")
            if chat_id is None:
                logger.warning(
                    "Reminder %s has no chat_id and no fallback destination",
                    reminder.get("id"),
                )
                reminder["status"] = "invalid"
                changed = True
                continue

            claim_token = uuid.uuid4().hex
            reminder["status"] = "sending"
            reminder["claim_token"] = claim_token
            reminder["claimed_at"] = now.isoformat()
            reminder["attempts"] = attempts + 1
            changed = True
            due.append({
                "id": reminder.get("id"),
                "claim_token": claim_token,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message": reminder.get("message", ""),
                "late_seconds": (now - fire_at).total_seconds(),
            })

        if changed:
            _save(reminders_file, reminders)

        return due


def _reconcile_delivery_results(reminders_file: Path, results: list[dict]) -> None:
    """Merge send outcomes back into the latest file contents."""
    if not results:
        return

    with _lock:
        reminders = _load(reminders_file)
        if not reminders:
            return

        changed = False
        for result in results:
            reminder = next(
                (
                    item for item in reminders
                    if item.get("id") == result["id"]
                    and item.get("claim_token") == result["claim_token"]
                ),
                None,
            )
            if reminder is None:
                logger.warning(
                    "Reminder %s changed before reconciliation; skipping stale claim",
                    result["id"],
                )
                continue

            if result["status"] == "sent":
                reminder["status"] = "sent"
                reminder["sent_at"] = result["sent_at"]
                late_by_seconds = result.get("late_by_seconds")
                if late_by_seconds is not None:
                    reminder["late_by_seconds"] = late_by_seconds
                else:
                    reminder.pop("late_by_seconds", None)
            else:
                reminder["status"] = "pending"
                reminder.pop("sent_at", None)
                reminder.pop("late_by_seconds", None)

            _clear_claim(reminder)
            changed = True

        if changed:
            _save(reminders_file, reminders)


def append_reminder(reminders_file: Path, entry: dict) -> None:
    """Append a single reminder entry to ``reminders.json``.

    Thread-safe (uses the same module lock as :func:`check_reminders`) and
    atomic (the underlying ``_save`` writes a temp file then renames). The
    file is created as ``[]`` if it does not yet exist. ``entry`` is taken
    as-is — callers are responsible for shaping it to match the schema
    that :func:`check_reminders` expects (``id``, ``message``, ``fire_at``,
    ``thread_id``, ``status``, ``created_at``).
    """
    with _lock:
        reminders = _load(reminders_file)
        reminders.append(entry)
        _save(reminders_file, reminders)


async def check_reminders(
    reminders_file: Path,
    platform: Any,
    default_chat_id: Any = None,
) -> None:
    """Check and fire any due reminders. Called every minute by the scheduler.

    Reminder entries created by modern handlers carry both ``chat_id`` and
    ``thread_id`` so the destination can be reconstructed on every platform.
    Legacy entries from the Telegram-only engine may not have ``chat_id``;
    callers can pass ``default_chat_id`` as a compatibility fallback.
    """
    due = _claim_due_reminders(
        reminders_file,
        default_chat_id=default_chat_id,
    )
    if not due:
        return

    results = []
    for reminder in due:
        try:
            sent = await asyncio.wait_for(
                platform.send_message(
                    chat_id=reminder["chat_id"],
                    text=reminder["message"],
                    thread_id=reminder["thread_id"],
                    parse_mode="markdown",
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
            if sent is None:
                logger.error(
                    "Failed to send reminder %s: platform returned no message ref",
                    reminder["id"],
                )
                results.append({
                    "id": reminder["id"],
                    "claim_token": reminder["claim_token"],
                    "status": "pending",
                })
                continue

            sent_at = datetime.now(timezone.utc)
            result = {
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "sent",
                "sent_at": sent_at.isoformat(),
            }
            if reminder["late_seconds"] > 120:
                result["late_by_seconds"] = int(reminder["late_seconds"])
                logger.info(
                    "Reminder %s fired (%.0fs late): %s",
                    reminder["id"],
                    reminder["late_seconds"],
                    reminder["message"][:60],
                )
            else:
                logger.info(
                    "Reminder %s fired on time: %s",
                    reminder["id"],
                    reminder["message"][:60],
                )
            results.append(result)
        except asyncio.TimeoutError:
            logger.error(
                "Reminder %s send timed out after %ds",
                reminder["id"],
                SEND_TIMEOUT_SECONDS,
            )
            results.append({
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "pending",
            })
        except (OSError, RuntimeError) as e:
            logger.error("Failed to send reminder %s: %s", reminder["id"], e)
            results.append({
                "id": reminder["id"],
                "claim_token": reminder["claim_token"],
                "status": "pending",
            })

    _reconcile_delivery_results(reminders_file, results)
