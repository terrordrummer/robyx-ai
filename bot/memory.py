"""Robyx — Agent memory system.

Two-tier memory: active (loaded into context) and archive (queryable on demand).
Backed by SQLite with FTS5 full-text search for crash-safe, indexed storage.

Workspace agents respect the project's existing memory system:
- If a project already has Claude Code memory (.claude/ with CLAUDE.md or memory
  files), Robyx does NOT inject its own memory — Claude Code handles it natively.
- If a project has NO existing memory, Robyx provides its own via SQLite databases.

Specialist and orchestrator memory always uses Robyx format:
- Specialist: {DATA_DIR}/memory/{name}.db
- Robyx:      {DATA_DIR}/memory/robyx.db
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR
from memory_store import (
    append_archive_entry,
    get_connection,
    list_archive_topics,
    load_active_snapshot,
    resolve_db_path,
    save_active_snapshot,
    search_archive as _store_search,
)

log = logging.getLogger("robyx.memory")

ACTIVE_FILE = "active.md"
ARCHIVE_DIR = "archive"
MAX_ACTIVE_WORDS = 5000


# ── Detection ──


def has_native_claude_memory(work_dir: str) -> bool:
    """Check if a project already has Claude Code's native memory/config structure."""
    project_root = Path(work_dir)
    if (project_root / "CLAUDE.md").exists():
        return True
    claude_dir = project_root / ".claude"
    if claude_dir.is_dir() and any(claude_dir.iterdir()):
        return True
    return False


# ── Path resolution ──


def get_memory_dir(agent_name: str, agent_type: str, work_dir: str) -> Path:
    """Return the memory directory for an agent.

    Kept for backward compatibility — callers that need the directory
    (e.g. for memory instructions) still use this.
    """
    if agent_type == "orchestrator" or agent_name == "robyx":
        return DATA_DIR / "memory" / "robyx"
    if agent_type == "specialist":
        return DATA_DIR / "memory" / agent_name
    return Path(work_dir) / ".robyx" / "memory"


def _get_db(agent_name: str, agent_type: str, work_dir: str):
    """Open a SQLite connection for the agent's memory DB."""
    db_path = resolve_db_path(agent_name, agent_type, work_dir, DATA_DIR)
    return get_connection(db_path)


# ── Load ──


def load_active(memory_dir: Path) -> str:
    """Load the active memory file. Returns empty string if not found.

    Legacy file-based interface — kept for backward compatibility with tests.
    Production code should use ``build_memory_context`` instead.
    """
    active_file = memory_dir / "active.md"
    if not active_file.exists():
        return ""
    try:
        return active_file.read_text().strip()
    except OSError as e:
        log.warning("Failed to read active memory %s: %s", active_file, e)
        return ""


def load_archive_index(memory_dir: Path) -> list[str]:
    """List available archive files (for on-demand queries).

    Legacy file-based interface — kept for backward compatibility.
    """
    archive_dir = memory_dir / "archive"
    if not archive_dir.exists():
        return []
    return sorted(f.stem for f in archive_dir.glob("*.md"))


# ── Save ──


def save_active(memory_dir: Path, content: str):
    """Write active memory. Creates directory if needed.

    Legacy file-based interface — kept for backward compatibility.
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    active_file = memory_dir / "active.md"
    active_file.write_text(content.strip() + "\n")
    log.info("Saved active memory: %s (%d words)", active_file, word_count(content))


def append_archive(memory_dir: Path, entry: str, reason: str = "obsolete"):
    """Append an entry to the current quarter's archive file.

    Legacy file-based interface — kept for backward compatibility.
    """
    archive_dir = memory_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    quarter = "Q%d" % ((now.month - 1) // 3 + 1)
    archive_file = archive_dir / ("%d-%s.md" % (now.year, quarter))

    header = "\n---\n_Archived: %s | Reason: %s_\n\n" % (
        now.strftime("%Y-%m-%d %H:%M UTC"), reason,
    )

    with open(archive_file, "a") as f:
        f.write(header + entry.strip() + "\n")

    log.info("Archived entry to %s (%s)", archive_file.name, reason)


# ── SQLite-backed public API ──


def search_memory(
    agent_name: str,
    agent_type: str,
    work_dir: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Full-text search across archived entries.

    Returns a list of dicts with keys:
    ``content``, ``topic``, ``created_at``, ``rank``.
    """
    conn = _get_db(agent_name, agent_type, work_dir)
    try:
        return _store_search(conn, agent_name, query, limit)
    finally:
        conn.close()


# ── Budget ──


def word_count(text: str) -> int:
    """Approximate word count."""
    return len(text.split())


def is_over_budget(text: str) -> bool:
    """Check if text exceeds the active memory word budget."""
    return word_count(text) > MAX_ACTIVE_WORDS


# ── Prompt building ──


def build_memory_context(agent_name: str, agent_type: str, work_dir: str) -> str:
    """Build the full memory context string to inject into an agent's prompt.

    Returns empty string if:
    - No memory exists
    - Workspace agent on a project that already has Claude Code native memory
      (Claude CLI handles that automatically — no injection needed)

    Reads fresh from SQLite on every call — no caching.  This is intentional:
    after context compaction the agent's prompt is rebuilt, and we must always
    serve the latest state.
    """
    if agent_type == "workspace" and agent_name != "robyx":
        if has_native_claude_memory(work_dir):
            return ""

    # Try SQLite first; fall back to legacy markdown if no DB exists.
    db_path = resolve_db_path(agent_name, agent_type, work_dir, DATA_DIR)
    if db_path.exists():
        conn = get_connection(db_path)
        try:
            active = load_active_snapshot(conn, agent_name)
            if not active:
                return ""

            topics = list_archive_topics(conn, agent_name)
            archive_note = ""
            if topics:
                archive_note = (
                    "\n_Archive available (%d topics): %s. "
                    "Use search to find specific past decisions or history._"
                    % (len(topics), ", ".join(topics))
                )

            return (
                "\n\n## Agent Memory\n\n"
                "The following is your active memory — a living document you "
                "maintain. It reflects the current state of your work as of "
                "the last update.\n\n"
                "%s%s" % (active, archive_note)
            )
        finally:
            conn.close()

    # Legacy fallback: markdown files.
    memory_dir = get_memory_dir(agent_name, agent_type, work_dir)
    active = load_active(memory_dir)
    if not active:
        return ""

    archives = load_archive_index(memory_dir)
    archive_note = ""
    if archives:
        archive_note = (
            "\n_Archive available (%d periods): %s. "
            "Query only if explicitly asked or if the task requires "
            "historical context._"
            % (len(archives), ", ".join(archives))
        )

    return (
        "\n\n## Agent Memory\n\n"
        "The following is your active memory — a living document you maintain. "
        "It reflects the current state of your work as of the last update.\n\n"
        "%s%s" % (active, archive_note)
    )


MEMORY_INSTRUCTIONS = """
## Memory Management

You have a persistent memory at `{memory_dir}`.

### How memory works
- Your working memory is loaded into your context at the start of every
  conversation.  It persists across context compaction events — when the AI
  backend compacts the conversation, your memory is re-injected automatically.
- Archive contains past entries you moved out.  You can **search** the archive
  by topic or keyword — ask "search memory for <topic>" to find past decisions.
- Memory survives bot restarts, updates, and migrations without data loss.

### When to update memory
There is no "session open/close" — conversations can be short, long,
interrupted, or span multiple topics. Update your memory **whenever something
worth remembering happens**:

- A decision is made or changed → update immediately
- A TODO is completed or added → update immediately
- The user says "remember X" → update immediately
- You finish a meaningful piece of work → update before responding
- You notice something in active memory is now wrong or stale → fix it now

Do NOT wait for a special moment. Do NOT batch updates. If the information
matters, write it now.

### What to write in active memory
Active memory is a snapshot of **what matters right now**. Write it for a
version of yourself that has never seen this project:

- **Current state**: What is this project? What's the current status?
- **Active decisions**: What was decided and WHY (not just what)
- **Open TODOs**: What needs to be done next?
- **Gotchas**: Known issues, fragile areas, things to avoid

### What NOT to write
- Code snippets — reference file paths and line numbers instead
- Completed work with no future relevance — archive it
- Anything derivable from the code itself (git log, file structure)

### Archiving obsolete information
When something in active memory is no longer relevant (completed TODO,
superseded decision, resolved issue), archive it with a reason so it
can be found later via search.

### Budget
Keep active memory under ~5000 words. If it's growing too long, that's a
signal to archive more aggressively. A concise memory is more useful than
a comprehensive one.
""".strip()


def get_memory_instructions(agent_name: str, agent_type: str, work_dir: str) -> str:
    """Return memory management instructions with the correct path filled in.

    Returns empty string for workspace agents on projects with native Claude Code
    memory — those projects already have their own memory management.
    """
    if agent_type == "workspace" and agent_name != "robyx":
        if has_native_claude_memory(work_dir):
            return ""

    memory_dir = get_memory_dir(agent_name, agent_type, work_dir)
    return MEMORY_INSTRUCTIONS.replace("{memory_dir}", str(memory_dir))
