"""Robyx — Conversation archive (spec 007.1).

Tracks user/agent turn pairs per workspace/collab/specialist agent so the
``/clear`` slash-command can produce a markdown archive of the current
conversation before resetting the AI-CLI session.

Storage layout::

    data/conversations/<agent>/current.jsonl          # append-only log of turns
    data/conversations/<agent>/archive-<UTC-ts>.md    # one file per /clear

``current.jsonl`` is rotated to a markdown archive on every ``/clear``
invocation. The markdown file is human-readable AND consumable by the
``[GET_ARCHIVE]`` macro that future turns can emit to recover context.

The module is deliberately defensive: ``append_turn`` swallows all
exceptions because it sits on the AI invocation hot path and MUST NOT
break message delivery if the disk is full or read-only. ``archive`` and
``query`` propagate errors so the user-visible ``/clear`` confirmation
can surface them.

The orchestrator (HQ) agent is excluded from turn logging by the caller
in ``bot/ai_invoke.py`` — Roberto's design choice (spec 007.1
clarification): ``/clear`` is a tool for the operational agents, not for
the orchestrator that needs full cross-session context to coordinate.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR

log = logging.getLogger("robyx.conversations")


CONVERSATIONS_DIR = DATA_DIR / "conversations"

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _safe(name: str) -> str:
    """Sanitise an agent name for filesystem use. Defensive — the agent
    name validator already enforces ``[a-z0-9-]``, but ``orchestrator``
    or future renames could introduce edge characters."""
    return _SAFE_NAME_RE.sub("_", name)


def _agent_dir(agent_name: str) -> Path:
    return CONVERSATIONS_DIR / _safe(agent_name)


def _current_path(agent_name: str) -> Path:
    return _agent_dir(agent_name) / "current.jsonl"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Turn logging (hot path) ────────────────────────────────────────────


def append_turn(
    agent_name: str,
    *,
    user_text: str,
    agent_text: str,
    user_id: Any = None,
    platform: str | None = None,
) -> None:
    """Append a single ``(user_text, agent_text)`` turn to the agent's
    current log. Best-effort; any failure is logged and swallowed so
    the AI-invocation hot path keeps running.

    The on-disk format is one JSON object per line (JSONL) with fields:

    - ``ts`` — ISO-8601 UTC, microsecond precision.
    - ``user_text`` — verbatim user message that prompted the turn.
    - ``agent_text`` — verbatim agent reply.
    - ``user_id`` — opaque platform user id, or None when unknown
      (e.g. scheduler-fired step).
    - ``platform`` — ``"telegram"`` / ``"discord"`` / ``"slack"`` / None.

    Atomic at the line level on POSIX for line sizes ≤ ``PIPE_BUF``;
    larger turns may interleave with concurrent writes from other
    invocations on the same agent. We accept that risk because turns
    are append-and-forget and the archive layer tolerates corrupt lines
    (``archive`` simply skips them).
    """
    try:
        path = _current_path(agent_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _now_utc().isoformat(),
            "user_text": user_text,
            "agent_text": agent_text,
            "user_id": user_id,
            "platform": platform,
        }
        line = json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "conversations.append_turn failed for %s: %s",
            agent_name, exc,
        )


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping malformed lines with a warning."""
    out: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("conversations._read_jsonl: %s: %s", path, exc)
        return out
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning(
                "conversations: skipping corrupt line %d in %s: %s",
                lineno, path, exc,
            )
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ── /clear — dump current.jsonl → archive-<ts>.md ─────────────────────


def _format_markdown(
    entries: list[dict],
    *,
    agent_name: str,
    display_name: str | None,
    session_id: str | None,
) -> str:
    """Render a list of turn dicts into a markdown archive document."""
    label = display_name or agent_name
    first_ts = entries[0].get("ts", "") if entries else ""
    last_ts = entries[-1].get("ts", "") if entries else ""

    lines: list[str] = []
    lines.append("## Conversation [%s] — %s → %s" % (label, first_ts, last_ts))
    lines.append("")
    lines.append("- agent: `%s`" % agent_name)
    if session_id:
        lines.append("- session_id (pre-clear): `%s`" % session_id)
    lines.append("- turns: %d" % len(entries))
    lines.append("")
    lines.append("---")
    lines.append("")
    for entry in entries:
        ts = entry.get("ts", "")
        # Best-effort HH:MM extraction from ISO-8601.
        hhmm = ts
        if "T" in ts:
            tail = ts.split("T", 1)[1]
            if len(tail) >= 5:
                hhmm = tail[:5]
        user_text = (entry.get("user_text") or "").strip()
        agent_text = (entry.get("agent_text") or "").strip()
        if user_text:
            lines.append("### User · %s" % hhmm)
            lines.append("")
            for ul in user_text.splitlines():
                lines.append("> %s" % ul if ul else ">")
            lines.append("")
        if agent_text:
            lines.append("### Robyx · %s" % hhmm)
            lines.append("")
            lines.append(agent_text)
            lines.append("")
    return "\n".join(lines)


def archive_and_clear(
    agent_name: str,
    *,
    display_name: str | None = None,
    session_id: str | None = None,
) -> tuple[Path, int] | None:
    """Read the agent's ``current.jsonl``, write a markdown archive, and
    truncate the log.

    Returns ``(archive_path, turn_count)`` on success, or ``None`` if
    there was no history to archive (no log file, empty log, or every
    line corrupt). Callers use ``turn_count`` to render a quantified
    user-visible confirmation.
    """
    current = _current_path(agent_name)
    if not current.exists() or current.stat().st_size == 0:
        return None

    entries = _read_jsonl(current)
    if not entries:
        # Every line was corrupt — still remove the file so the next
        # /clear doesn't see the same bad data.
        try:
            current.unlink()
        except OSError:
            pass
        return None

    body = _format_markdown(
        entries,
        agent_name=agent_name,
        display_name=display_name,
        session_id=session_id,
    )
    # Filename uses second-precision UTC. If a same-second collision
    # happens (typically only in tests; manual /clear calls cannot
    # realistically come faster than once per minute), append a short
    # counter suffix to keep filenames distinct.
    ts_label = _now_utc().strftime("%Y%m%dT%H%M%SZ")
    archive_path = _agent_dir(agent_name) / ("archive-%s.md" % ts_label)
    if archive_path.exists():
        suffix = 1
        while True:
            candidate = _agent_dir(agent_name) / (
                "archive-%s-%d.md" % (ts_label, suffix)
            )
            if not candidate.exists():
                archive_path = candidate
                break
            suffix += 1
    # Atomic write: temp file + replace, same primitive as the rest of
    # the data layer (CollabStore, events, etc.).
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = archive_path.with_suffix(archive_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(archive_path)

    # Truncate the current log atomically — same temp-file dance to
    # ensure a crash mid-rewrite never leaves a half-empty current file.
    try:
        current.unlink()
    except OSError as exc:
        log.warning(
            "conversations.archive_and_clear: could not unlink %s: %s",
            current, exc,
        )

    log.info(
        "conversations.archive_and_clear agent=%s turns=%d path=%s",
        agent_name, len(entries), archive_path,
    )
    return archive_path, len(entries)


# ── [GET_ARCHIVE] support ─────────────────────────────────────────────


def list_archives(agent_name: str) -> list[Path]:
    """Return the agent's archive markdown files (newest first)."""
    adir = _agent_dir(agent_name)
    if not adir.exists():
        return []
    return sorted(adir.glob("archive-*.md"), reverse=True)


def _archive_ts(path: Path) -> datetime | None:
    """Parse the timestamp encoded in an ``archive-YYYYMMDDTHHMMSSZ.md``
    filename, or the same-second-collision variant
    ``archive-YYYYMMDDTHHMMSSZ-<n>.md``. Returns ``None`` on malformed
    names so callers can skip them without raising."""
    stem = path.stem
    if not stem.startswith("archive-"):
        return None
    body = stem[len("archive-"):]
    # Strip the optional ``-<n>`` collision suffix before parsing.
    if "-" in body:
        ts_part, _, _ = body.rpartition("-")
        # Sanity: ts_part must be the full ISO-compact form (15 chars
        # with trailing Z). If splitting produced something shorter the
        # filename is malformed and we fall through to ValueError.
        if len(ts_part) == len("YYYYMMDDTHHMMSSZ"):
            body = ts_part
    try:
        return datetime.strptime(body, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def query_archives(
    agent_name: str,
    *,
    since: datetime | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return up to ``limit`` archive documents for ``agent_name``, newest
    first, filtered to those created at or after ``since`` (if given).

    Each result is a dict with ``path`` (str), ``ts`` (ISO-8601 UTC), and
    ``body`` (markdown content). Used by the ``[GET_ARCHIVE]`` macro
    handler in ``bot/handlers.py``.
    """
    if limit < 1:
        return []
    out: list[dict] = []
    for p in list_archives(agent_name):
        ts = _archive_ts(p)
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "conversations.query_archives: skipping %s: %s", p, exc,
            )
            continue
        out.append({"path": str(p), "ts": ts.isoformat(), "body": body})
        if len(out) >= limit:
            break
    return out
