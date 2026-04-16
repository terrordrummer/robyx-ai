"""0.20.28 -> 0.21.0 -- SQLite-backed memory engine.

Migrates the markdown-based agent memory system (active.md + archive/)
to SQLite databases with FTS5 full-text search.  Original markdown files
are renamed to ``.md.bak`` after successful migration.

The migration scans for all known agent memory directories and converts
each one independently.  If no markdown files exist (fresh install or
already migrated), the migration is a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Migration, MigrationContext

log = logging.getLogger("robyx.migrations.v0_21_0")


async def upgrade(ctx: MigrationContext) -> None:
    from memory_store import migrate_markdown_to_sqlite, resolve_db_path

    data_dir = ctx.data_dir
    if data_dir is None:
        return

    # ── Orchestrator (robyx) ──
    robyx_mem = data_dir / "memory" / "robyx"
    if robyx_mem.exists():
        db_path = resolve_db_path("robyx", "orchestrator", "", data_dir)
        if migrate_markdown_to_sqlite(db_path, "robyx", robyx_mem):
            log.info("Migrated orchestrator memory to SQLite")

    # ── Specialists ──
    memory_root = data_dir / "memory"
    if memory_root.exists():
        for subdir in sorted(memory_root.iterdir()):
            if not subdir.is_dir() or subdir.name == "robyx":
                continue
            agent_name = subdir.name
            db_path = resolve_db_path(agent_name, "specialist", "", data_dir)
            if migrate_markdown_to_sqlite(db_path, agent_name, subdir):
                log.info("Migrated specialist '%s' memory to SQLite", agent_name)

    # ── Workspace agents ──
    # Workspace memory lives inside project dirs, which we discover from
    # state.json if it exists.
    state_file = data_dir / "state.json"
    if state_file.exists():
        try:
            import json
            state = json.loads(state_file.read_text())
            agents = state.get("agents", {})
            for name, info in agents.items():
                work_dir = info.get("work_dir", "")
                if not work_dir:
                    continue
                ws_mem = Path(work_dir) / ".robyx" / "memory"
                if ws_mem.exists():
                    db_path = resolve_db_path(name, "workspace", work_dir, data_dir)
                    if migrate_markdown_to_sqlite(db_path, name, ws_mem):
                        log.info(
                            "Migrated workspace '%s' memory to SQLite", name
                        )
        except Exception as exc:
            log.warning("Could not migrate workspace memories: %s", exc)


MIGRATION = Migration(
    from_version="0.20.28",
    to_version="0.21.0",
    description="SQLite-backed memory engine with FTS5 full-text search",
    upgrade=upgrade,
)
