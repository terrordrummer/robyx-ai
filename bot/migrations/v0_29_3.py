"""0.29.2 → 0.29.3 — retire the legacy periodic system monitor.

Robyx stopped seeding the chatty six-hour system monitor in v0.18.0, but
installations that had already migrated it into ``data/queue.json`` kept the
recurring entry indefinitely.  This migration removes only matching
``periodic`` records.  It deliberately preserves one-shot diagnostics,
history, agent instructions, and every unrelated queue entry.

Queue recovery remains fail-closed: corrupt input is quarantined/recovered by
the shared persistence boundary and is never replaced with an apparently
clean queue derived from incomplete state.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from persistence_recovery import guard_json_write, load_json_with_recovery

from .base import Migration, MigrationContext

DEFAULT_LOG = logging.getLogger("robyx.migrations.v0_29_3")


def _resolve_data_dir(ctx: MigrationContext) -> Path:
    if ctx.data_dir is not None:
        return Path(ctx.data_dir)
    from config import DATA_DIR as configured_data_dir  # type: ignore

    return Path(configured_data_dir)


def _is_legacy_system_monitor(entry: dict[str, Any]) -> bool:
    """Match only recurring telemetry records, never ad-hoc diagnostics."""
    if entry.get("type") != "periodic":
        return False
    name = entry.get("name")
    agent_file = entry.get("agent_file")
    normalized_name = name.strip().casefold() if isinstance(name, str) else ""
    normalized_agent = (
        agent_file.strip().replace("\\", "/").casefold()
        if isinstance(agent_file, str)
        else ""
    )
    return (
        normalized_name == "system-monitor"
        or normalized_agent == "agents/system-monitor.md"
    )


def _atomic_write_queue(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".v0_29_3.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entries, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


async def upgrade(ctx: MigrationContext) -> None:
    # Import lazily so migration discovery stays lightweight.  The scheduler's
    # public validator is the single semantic contract for live queue records.
    from scheduler import valid_queue_payload

    log = ctx.log or DEFAULT_LOG
    data_dir = _resolve_data_dir(ctx)
    queue_path = data_dir / "queue.json"
    result = load_json_with_recovery(
        queue_path,
        data_dir=data_dir,
        validator=valid_queue_payload,
        kind="scheduler queue",
        logger=log,
    )
    if result.status == "missing":
        log.info("migration v0_29_3: no queue.json — nothing to retire")
        return

    entries = result.value
    retained = [entry for entry in entries if not _is_legacy_system_monitor(entry)]
    removed = len(entries) - len(retained)
    if not removed:
        log.info("migration v0_29_3: no periodic system monitor found")
        return

    guard_json_write(
        queue_path,
        data_dir=data_dir,
        validator=valid_queue_payload,
        kind="scheduler queue",
        logger=log,
    )
    _atomic_write_queue(queue_path, retained)
    log.info(
        "migration v0_29_3: retired %d periodic system-monitor queue entr%s",
        removed,
        "y" if removed == 1 else "ies",
    )


MIGRATION = Migration(
    from_version="0.29.2",
    to_version="0.29.3",
    description="retire legacy periodic system-monitor notifications",
    upgrade=upgrade,
)
