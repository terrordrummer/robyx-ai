"""Robyx — Agent memory system.

Two-tier memory: active (loaded into context) and archive (queryable on demand).

Workspace agents respect the project's existing memory system:
- If a project already has Claude Code memory (.claude/ with CLAUDE.md or memory
  files), Robyx does NOT inject its own memory — Claude Code handles it natively.
- If a project has NO existing memory, Robyx provides its own at
  {project_dir}/.robyx/memory/active.md + archive/

Specialist and orchestrator memory always uses Robyx format:
- Specialist: {DATA_DIR}/memory/{name}/active.md + archive/
- Robyx:      {DATA_DIR}/memory/robyx/active.md + archive/
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

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

    - orchestrator (robyx): DATA_DIR/memory/robyx/
    - specialist:           DATA_DIR/memory/{name}/
    - workspace:            {work_dir}/.robyx/memory/
    """
    if agent_type == "orchestrator" or agent_name == "robyx":
        return DATA_DIR / "memory" / "robyx"
    if agent_type == "specialist":
        return DATA_DIR / "memory" / agent_name
    # Workspace: memory lives inside the project
    return Path(work_dir) / ".robyx" / "memory"


# ── Load ──


def load_active(memory_dir: Path) -> str:
    """Load the active memory file. Returns empty string if not found."""
    active_file = memory_dir / ACTIVE_FILE
    if not active_file.exists():
        return ""
    try:
        return active_file.read_text().strip()
    except OSError as e:
        log.warning("Failed to read active memory %s: %s", active_file, e)
        return ""


def load_archive_index(memory_dir: Path) -> list[str]:
    """List available archive files (for on-demand queries)."""
    archive_dir = memory_dir / ARCHIVE_DIR
    if not archive_dir.exists():
        return []
    return sorted(f.stem for f in archive_dir.glob("*.md"))


# ── Save ──


def save_active(memory_dir: Path, content: str):
    """Write active memory. Creates directory if needed."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    active_file = memory_dir / ACTIVE_FILE
    active_file.write_text(content.strip() + "\n")
    log.info("Saved active memory: %s (%d words)", active_file, word_count(content))


def append_archive(memory_dir: Path, entry: str, reason: str = "obsolete"):
    """Append an entry to the current quarter's archive file."""
    archive_dir = memory_dir / ARCHIVE_DIR
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
    """
    if agent_type == "workspace" and agent_name != "robyx":
        if has_native_claude_memory(work_dir):
            return ""

    memory_dir = get_memory_dir(agent_name, agent_type, work_dir)
    active = load_active(memory_dir)
    if not active:
        return ""

    archives = load_archive_index(memory_dir)
    archive_note = ""
    if archives:
        archive_note = (
            "\n_Archive available (%d periods): %s. "
            "Query only if explicitly asked or if the task requires historical context._"
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
- `active.md` is your working memory — it's loaded into your context at the start of every conversation.
- `archive/` contains past entries you moved out because they became obsolete. Not loaded by default.

### When to update memory
There is no "session open/close" — conversations can be short, long, interrupted, or span multiple topics. Update your memory **whenever something worth remembering happens**:

- A decision is made or changed → update immediately
- A TODO is completed or added → update immediately
- The user says "remember X" → update immediately
- You finish a meaningful piece of work → update before responding
- You notice something in active.md is now wrong or stale → fix it now

Do NOT wait for a special moment. Do NOT batch updates. If the information matters, write it now.

### What to write in active.md
Active memory is a snapshot of **what matters right now**. Write it for a version of yourself that has never seen this project:

- **Current state**: What is this project? What's the current status?
- **Active decisions**: What was decided and WHY (not just what)
- **Open TODOs**: What needs to be done next?
- **Gotchas**: Known issues, fragile areas, things to avoid

### What NOT to write
- Code snippets — reference file paths and line numbers instead
- Completed work with no future relevance — archive it
- Anything derivable from the code itself (git log, file structure)

### Archiving obsolete information
When something in active.md is no longer relevant (completed TODO, superseded decision, resolved issue):

1. Append it to `{memory_dir}/archive/YYYY-QN.md` (e.g., `2026-Q2.md`) with:
   `---`
   `_Archived: <date> | Reason: <why>_`
   followed by the entry
2. Remove it from active.md

Archive is append-only — never modify archived entries.

### Budget
Keep active.md under ~5000 words. If it's growing too long, that's a signal to archive more aggressively. A concise memory is more useful than a comprehensive one.
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
    return MEMORY_INSTRUCTIONS.format(memory_dir=memory_dir)
