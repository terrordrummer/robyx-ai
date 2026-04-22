"""Robyx — append-only event journal (spec 006).

The event journal is the single chokepoint through which scheduler and
lifecycle activity becomes queryable history. Orchestrator agents read it
on demand via the ``[GET_EVENTS]`` macro; HQ receives zero automatic
push-notifications for routine events (FR-002). Only events meeting the
FR-002a last-resort criteria surface in HQ.

The storage layer is deliberately minimal:

* One hot JSONL file at ``data/events.jsonl``.
* Rotated shards at ``data/events/events-YYYYMMDD-HH.jsonl``.
* 7-day retention (shards older than ``EVENT_RETENTION_DAYS`` are pruned
  on each rotation pass).
* One append ≤ ``PIPE_BUF`` is atomic on POSIX, so concurrent readers
  never see a torn line.

The record schema is task-type-agnostic from day one (``task_type`` field
is mandatory). This avoids a second migration when we later extend
coverage to periodic / one-shot / reminder tasks.

Contracts: ``specs/006-continuous-task-robustness/contracts/event-journal.md``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("robyx.events")


# ── Module state ─────────────────────────────────────────────────────────────

_append_lock = threading.Lock()

# Config resolution is lazy so unit tests can patch paths on a per-call basis.


def _config() -> tuple[Path, Path, int, int]:
    """Return (hot_file, events_dir, retention_days, max_hot_bytes)."""
    from config import (
        EVENTS_DIR,
        EVENTS_HOT_FILE,
        EVENT_MAX_HOT_BYTES,
        EVENT_RETENTION_DAYS,
    )

    return (
        Path(EVENTS_HOT_FILE),
        Path(EVENTS_DIR),
        int(EVENT_RETENTION_DAYS),
        int(EVENT_MAX_HOT_BYTES),
    )


# ── Taxonomy ─────────────────────────────────────────────────────────────────

VALID_TASK_TYPES = frozenset({"continuous", "periodic", "one-shot", "reminder"})

# Known continuous-task event types (MVP scope). Unknown types are tolerated
# on both append and read — forward-compat without a schema migration.
KNOWN_EVENT_TYPES = frozenset({
    "created", "dispatched", "step_start", "step_complete",
    "state_transition", "stopped", "resumed", "completed", "deleted",
    "archived", "error", "orphan_detected", "orphan_incident",
    "orphan_recovery", "rate_limited", "rate_limit_recovered",
    "lock_heartbeat_stale", "lock_recovered", "drain_started",
    "drain_completed", "drain_timeout", "hq_fallback_sent", "migration",
    "awaiting_reminder_sent", "topic_recreated",
    # Reserved hook points for non-continuous tasks (no-op in MVP).
    "periodic_fired", "periodic_completed",
    "oneshot_fired", "oneshot_completed",
    "reminder_fired", "reminder_delivered", "reminder_failed",
})


# ── Internals ────────────────────────────────────────────────────────────────

_MAX_PAYLOAD_BYTES = 1024  # truncation threshold for payload serialisation


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _serialise_payload(payload: Optional[dict]) -> dict:
    if not payload:
        return {}
    try:
        raw = json.dumps(payload, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return {"_serialisation_error": True}
    if len(raw.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        return {"_truncated": True}
    return payload


def _hot_hour_key(path: Path) -> Optional[str]:
    """Return the 'YYYYMMDD-HH' key of the first line's timestamp, or None
    if the file is empty/unreadable. Used to decide hourly rotation.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        entry = json.loads(first)
        ts = datetime.fromisoformat(entry["ts"])
    except (ValueError, KeyError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y%m%d-%H")


def _shard_path(events_dir: Path, hour_key: str) -> Path:
    return events_dir / ("events-%s.jsonl" % hour_key)


# ── Public API ───────────────────────────────────────────────────────────────


def append(
    task_name: str,
    task_type: str,
    event_type: str,
    outcome: str,
    payload: Optional[dict] = None,
) -> None:
    """Append a single event to the hot journal.

    Thread-safe. Silent on disk errors (logs WARN and returns without
    raising) so a journal append failure never crashes a dispatch path.
    """
    if task_type not in VALID_TASK_TYPES:
        log.warning(
            "events.append: unknown task_type=%r (task=%r event=%r) — "
            "using 'continuous' default",
            task_type, task_name, event_type,
        )
        task_type = "continuous"

    entry = {
        "ts": _iso(_now_utc()),
        "task_name": str(task_name or ""),
        "task_type": task_type,
        "event_type": str(event_type),
        "outcome": str(outcome),
        "payload": _serialise_payload(payload),
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    hot, events_dir, _, _ = _config()

    with _append_lock:
        try:
            hot.parent.mkdir(parents=True, exist_ok=True)
            events_dir.mkdir(parents=True, exist_ok=True)
            with hot.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error(
                "events.append: disk error while writing (%s) — event dropped: "
                "task=%r type=%r",
                exc, task_name, event_type,
            )


def query(
    since: datetime,
    task_name: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Return events within [since, now], newest first, capped at ``limit``.

    Scans the hot file plus any rotated shards whose hour key overlaps the
    requested window. Filters by ``task_name`` / ``event_type`` in-memory.
    ``limit`` is clamped to [1, 1000].
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    limit = max(1, min(int(limit), 1000))

    hot, events_dir, _, _ = _config()

    # Collect candidate files: hot + shards whose hour-key is ≥ floor-of-since.
    candidates: list[Path] = []
    if events_dir.exists():
        for shard in sorted(events_dir.iterdir()):
            if not shard.is_file() or not shard.name.startswith("events-"):
                continue
            # events-YYYYMMDD-HH.jsonl
            stem = shard.stem  # events-YYYYMMDD-HH
            parts = stem.split("-")
            if len(parts) != 3:
                continue
            _, date_part, hour_part = parts
            try:
                shard_start = datetime.strptime(
                    "%s%s" % (date_part, hour_part),
                    "%Y%m%d%H",
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            shard_end = shard_start + timedelta(hours=1)
            if shard_end < since:
                continue
            candidates.append(shard)
    if hot.exists():
        candidates.append(hot)

    results: list[dict] = []
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_num, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning(
                            "events.query: skipping corrupted line in %s:%d",
                            path.name, line_num,
                        )
                        continue
                    try:
                        ts = datetime.fromisoformat(entry["ts"])
                    except (KeyError, ValueError):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since:
                        continue
                    if task_name is not None and entry.get("task_name") != task_name:
                        continue
                    if event_type is not None and entry.get("event_type") != event_type:
                        continue
                    results.append(entry)
        except OSError as exc:
            log.warning("events.query: read error on %s: %s", path, exc)

    # Newest-first ordering.
    def _sort_key(e: dict) -> str:
        return e.get("ts", "")

    results.sort(key=_sort_key, reverse=True)
    return results[:limit]


def rotate_if_needed() -> Optional[Path]:
    """Rotate the hot file to a dated shard if either (a) the hour has
    changed since the first line was written, or (b) the hot file exceeds
    ``EVENT_MAX_HOT_BYTES``. Also prunes shards older than
    ``EVENT_RETENTION_DAYS``.

    Returns the rotated shard path (if any rotation occurred), else None.
    Safe to call every scheduler cycle.
    """
    hot, events_dir, retention_days, max_hot_bytes = _config()
    rotated: Optional[Path] = None

    with _append_lock:
        should_rotate = False
        if hot.exists() and hot.stat().st_size > 0:
            try:
                size = hot.stat().st_size
            except OSError:
                size = 0
            if size > max_hot_bytes:
                should_rotate = True
            else:
                hour_key = _hot_hour_key(hot)
                now_key = _now_utc().strftime("%Y%m%d-%H")
                if hour_key is not None and hour_key != now_key:
                    should_rotate = True

        if should_rotate:
            events_dir.mkdir(parents=True, exist_ok=True)
            hour_key = _hot_hour_key(hot) or _now_utc().strftime("%Y%m%d-%H")
            target = _shard_path(events_dir, hour_key)

            # On hour-change rotation, append to an existing shard rather
            # than clobber (safeguard against clock jumps).
            if target.exists():
                try:
                    with hot.open("r", encoding="utf-8") as src, \
                         target.open("a", encoding="utf-8") as dst:
                        for line in src:
                            dst.write(line)
                    hot.unlink(missing_ok=True)
                    hot.touch()
                    rotated = target
                except OSError as exc:
                    log.error("events.rotate: merge failed on %s: %s", target, exc)
            else:
                try:
                    os.replace(str(hot), str(target))
                    hot.touch()
                    rotated = target
                except OSError as exc:
                    log.error(
                        "events.rotate: rename %s → %s failed: %s",
                        hot, target, exc,
                    )

    # Prune retention outside the append lock — shard deletions are
    # independent of writes.
    prune_retention(max_age_days=retention_days)
    return rotated


def prune_retention(max_age_days: int = 7) -> int:
    """Delete shards older than ``max_age_days``. Returns count removed."""
    _, events_dir, _, _ = _config()
    if not events_dir.exists():
        return 0

    cutoff = _now_utc() - timedelta(days=max(0, int(max_age_days)))
    removed = 0
    for shard in sorted(events_dir.iterdir()):
        if not shard.is_file() or not shard.name.startswith("events-"):
            continue
        stem = shard.stem
        parts = stem.split("-")
        if len(parts) != 3:
            continue
        _, date_part, hour_part = parts
        try:
            shard_start = datetime.strptime(
                "%s%s" % (date_part, hour_part),
                "%Y%m%d%H",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if shard_start < cutoff:
            try:
                shard.unlink()
                removed += 1
            except OSError as exc:
                log.warning(
                    "events.prune_retention: unlink failed on %s: %s",
                    shard, exc,
                )
    if removed:
        log.info("events.prune_retention: removed %d stale shard(s)", removed)
    return removed
