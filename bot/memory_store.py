"""Robyx — SQLite-backed memory storage layer.

Provides ACID-safe persistence for agent memory using SQLite with FTS5
full-text search.  Each agent gets its own ``.db`` file.  WAL mode is
enabled for crash safety and concurrent reads.

This module is the *storage* layer.  ``memory.py`` remains the public
API; it delegates to functions here instead of raw file I/O.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger("robyx.memory_store")

# ── Schema ──

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS active_snapshots (
    agent_name  TEXT PRIMARY KEY,
    content     TEXT NOT NULL DEFAULT '',
    word_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'archive',
    content         TEXT NOT NULL,
    topic           TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ', 'now')),
    archived_at     TEXT DEFAULT NULL,
    archive_reason  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_entries_agent
    ON entries(agent_name);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_entries USING fts5(
    content,
    topic,
    tags,
    content='entries',
    content_rowid='id'
);

-- Triggers to keep FTS index in sync with entries table.
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO fts_entries(rowid, content, topic, tags)
    VALUES (new.id, new.content, new.topic, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO fts_entries(fts_entries, rowid, content, topic, tags)
    VALUES ('delete', old.id, old.content, old.topic, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO fts_entries(fts_entries, rowid, content, topic, tags)
    VALUES ('delete', old.id, old.content, old.topic, old.tags);
    INSERT INTO fts_entries(rowid, content, topic, tags)
    VALUES (new.id, new.content, new.topic, new.tags);
END;
"""


# ── Connection management ──


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open (or create) a SQLite database at *db_path* with WAL mode.

    Creates the parent directory and schema on first access.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
    except Exception:
        # Don't leak the file descriptor if PRAGMA/schema setup fails
        # (disk full, read-only FS, corrupt DB, etc.).
        conn.close()
        raise
    return conn


# ── Path resolution ──


def _validated_db_name_segment(agent_name: str) -> str:
    """Reject agent names that would escape their memory directory.

    Upstream call sites (``topics.create_workspace``, ``create_specialist``,
    collab Flow B bootstrap) already sanitise via ``_sanitize_task_name``.
    This is defense-in-depth for any future call site or a tampered
    ``state.json`` — same approach as ``task_runtime.validate_task_name``.
    """
    value = str(agent_name or "").strip()
    if not value:
        raise ValueError("agent_name is required for memory DB resolution")
    if any(ch in value for ch in ("\n", "\r", "\t", "\0", "/", "\\")):
        raise ValueError(
            "agent_name contains unsupported characters: %r" % agent_name
        )
    if value in (".", ".."):
        raise ValueError("agent_name cannot be '.' or '..'")
    return value


def resolve_db_path(
    agent_name: str,
    agent_type: str,
    work_dir: str,
    data_dir: Path,
) -> Path:
    """Map agent identity to a ``.db`` file path.

    * orchestrator / robyx  → ``{data_dir}/memory/robyx.db``
    * specialist            → ``{data_dir}/memory/{name}.db``
    * workspace             → ``{work_dir}/.robyx/memory.db``

    Raises ``ValueError`` if ``agent_name`` would escape the memory dir
    (contains path separators, control chars, or is ``.`` / ``..``).
    """
    if agent_type == "orchestrator" or agent_name == "robyx":
        return data_dir / "memory" / "robyx.db"
    if agent_type == "specialist":
        safe = _validated_db_name_segment(agent_name)
        return data_dir / "memory" / f"{safe}.db"
    return Path(work_dir) / ".robyx" / "memory.db"


# ── Active snapshots ──


def load_active_snapshot(conn: sqlite3.Connection, agent_name: str) -> str:
    """Load the current active memory text.  Returns ``""`` if none."""
    row = conn.execute(
        "SELECT content FROM active_snapshots WHERE agent_name = ?",
        (agent_name,),
    ).fetchone()
    return row["content"].strip() if row else ""


def save_active_snapshot(
    conn: sqlite3.Connection,
    agent_name: str,
    content: str,
) -> None:
    """Atomically save (upsert) the active memory snapshot."""
    text = content.strip()
    wc = len(text.split())
    conn.execute(
        """INSERT INTO active_snapshots (agent_name, content, word_count, updated_at)
           VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
           ON CONFLICT(agent_name) DO UPDATE SET
               content    = excluded.content,
               word_count = excluded.word_count,
               updated_at = excluded.updated_at""",
        (agent_name, text, wc),
    )
    conn.commit()
    log.info("Saved active snapshot for %s (%d words)", agent_name, wc)


# ── Archive entries ──


def append_archive_entry(
    conn: sqlite3.Connection,
    agent_name: str,
    content: str,
    reason: str = "obsolete",
    topic: str = "",
    tags: str = "",
) -> int:
    """Insert an archive entry.  Returns the new row id."""
    cur = conn.execute(
        """INSERT INTO entries
               (agent_name, tier, content, topic, tags,
                archived_at, archive_reason)
           VALUES (?, 'archive', ?, ?, ?,
                   strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?)""",
        (agent_name, content.strip(), topic, tags, reason),
    )
    conn.commit()
    row_id = cur.lastrowid
    log.info(
        "Archived entry %d for %s (topic=%s, reason=%s)",
        row_id, agent_name, topic or "(none)", reason,
    )
    return row_id


def search_archive(
    conn: sqlite3.Connection,
    agent_name: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Full-text search across archived entries using FTS5 + BM25 ranking.

    Returns a list of dicts with keys:
    ``content``, ``topic``, ``created_at``, ``rank``.
    """
    # Sanitise the query for FTS5: strip special characters, keep words.
    clean = re.sub(r"[^\w\s*]", "", query).strip()
    if not clean:
        return []

    # Add prefix matching to each word for broader recall.
    terms = " OR ".join(f'"{w}"*' for w in clean.split())

    rows = conn.execute(
        """SELECT e.content, e.topic, e.created_at,
                  bm25(fts_entries) AS rank
           FROM fts_entries
           JOIN entries e ON e.id = fts_entries.rowid
           WHERE fts_entries MATCH ?
             AND e.agent_name = ?
             AND e.tier = 'archive'
           ORDER BY rank
           LIMIT ?""",
        (terms, agent_name, limit),
    ).fetchall()

    return [
        {
            "content": r["content"],
            "topic": r["topic"],
            "created_at": r["created_at"],
            "rank": r["rank"],
        }
        for r in rows
    ]


def list_archive_topics(
    conn: sqlite3.Connection,
    agent_name: str,
) -> list[str]:
    """Return distinct topic strings for an agent's archived entries."""
    rows = conn.execute(
        """SELECT DISTINCT topic FROM entries
           WHERE agent_name = ? AND tier = 'archive' AND topic != ''
           ORDER BY topic""",
        (agent_name,),
    ).fetchall()
    return [r["topic"] for r in rows]


def aggregate_active_summaries(
    db_paths: dict[str, Path],
) -> dict[str, str]:
    """Read active snapshots from multiple agent DBs.

    *db_paths* maps agent names to their ``.db`` file paths.
    Returns ``{agent_name: content}`` for agents that have content.
    Skips agents whose DB does not exist.
    """
    result: dict[str, str] = {}
    for name, path in db_paths.items():
        if not path.exists():
            continue
        try:
            conn = get_connection(path)
            try:
                text = load_active_snapshot(conn, name)
                if text:
                    result[name] = text
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("Failed to read memory for %s: %s", name, exc)
    return result


# ── Migration helper ──


def migrate_markdown_to_sqlite(
    db_path: Path,
    agent_name: str,
    memory_dir: Path,
) -> bool:
    """Migrate markdown memory files into a SQLite database.

    Parses ``active.md`` and ``archive/*.md`` under *memory_dir*,
    inserts them into the database at *db_path*, and renames the
    original files to ``.md.bak``.

    Returns ``True`` if migration was performed, ``False`` if there
    was nothing to migrate (no markdown files found).
    """
    active_file = memory_dir / "active.md"
    archive_dir = memory_dir / "archive"

    has_active = active_file.exists()
    has_archive = archive_dir.exists() and any(archive_dir.glob("*.md"))

    if not has_active and not has_archive:
        return False

    conn = get_connection(db_path)
    try:
        # ── Active memory ──
        if has_active:
            content = active_file.read_text().strip()
            if content:
                save_active_snapshot(conn, agent_name, content)
                log.info("Migrated active.md for %s", agent_name)
            active_file.rename(active_file.with_suffix(".md.bak"))

        # ── Archive entries ──
        if has_archive:
            for md_file in sorted(archive_dir.glob("*.md")):
                raw = md_file.read_text()
                entries = _split_archive_entries(raw)
                for entry_text, reason, timestamp in entries:
                    if not entry_text.strip():
                        continue
                    conn.execute(
                        """INSERT INTO entries
                               (agent_name, tier, content, topic, tags,
                                created_at, archived_at, archive_reason)
                           VALUES (?, 'archive', ?, '', '',
                                   ?, ?, ?)""",
                        (
                            agent_name,
                            entry_text.strip(),
                            timestamp or "",
                            timestamp or "",
                            reason or "migrated",
                        ),
                    )
                conn.commit()
                log.info(
                    "Migrated %d entries from %s",
                    len(entries), md_file.name,
                )
                md_file.rename(md_file.with_suffix(".md.bak"))

        return True
    finally:
        conn.close()


def _split_archive_entries(
    raw: str,
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Split a quarterly archive file into individual entries.

    Each entry is preceded by a ``---`` divider and a metadata line like::

        _Archived: 2026-04-09 14:30 UTC | Reason: superseded_

    Returns a list of ``(content, reason, timestamp)`` tuples.
    """
    blocks = re.split(r"\n---\n", raw)
    results: list[tuple[str, Optional[str], Optional[str]]] = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        reason = None
        timestamp = None

        # Try to extract the metadata line.
        meta_match = re.match(
            r"_Archived:\s*(.+?)\s*\|\s*Reason:\s*(.+?)_\s*\n?(.*)",
            block,
            re.DOTALL,
        )
        if meta_match:
            timestamp = meta_match.group(1).strip()
            reason = meta_match.group(2).strip()
            block = meta_match.group(3).strip()

        if block:
            results.append((block, reason, timestamp))

    return results
