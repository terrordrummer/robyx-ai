"""0.24.2 → 0.24.3 — normalise continuous-task history schema.

Pre-0.24.3 scheduler code did a hard dict access (``entry["description"]``)
when building the step-history context for the secondary step agent. A
step agent that drifted from the documented schema — most commonly
writing ``summary`` instead of ``description`` — would cause a KeyError
that bubbled out of ``_handle_continuous_entries`` and left every
continuous task undispatched on every scheduler tick.

0.24.3 fixes the reader to tolerate the drift, but we also normalise
any existing state files once so the data-at-rest matches the documented
schema. For each ``data/continuous/<name>/state.json`` found on disk:

- For every entry in ``history`` that lacks a ``description`` key but
  carries ``summary``, rename ``summary`` → ``description`` in place.
- Do not touch entries that already have ``description`` (even if they
  also carry ``summary``) — the reader prefers ``description`` anyway
  and the extra key is harmless.
- Do not touch entries that have neither — the reader falls back to
  ``artifact`` or a placeholder; rewriting arbitrary keys here would
  hide further drift from future audits.

Idempotency: the migration is safe to run multiple times. Once a state
file has been normalised, subsequent runs are no-ops (no ``summary``
keys left to rename). A state file that contains neither key remains
untouched on each pass.

Write atomicity mirrors ``continuous.save_state``: write-to-temp,
``fsync``, ``os.replace``. Parse errors and I/O errors on individual
state files are logged and skipped — one broken task must not block the
rest of the migration.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .base import Migration, MigrationContext


DEFAULT_LOG = logging.getLogger("robyx.migrations.v0_24_3")


def _resolve_continuous_dir(ctx: MigrationContext) -> Path:
    """Return the ``data/continuous`` directory for this runtime."""
    if ctx.data_dir is not None:
        return Path(ctx.data_dir) / "continuous"
    from config import CONTINUOUS_DIR as _CONTINUOUS_DIR  # type: ignore
    return Path(_CONTINUOUS_DIR)


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _normalise_history(history: list, log: logging.Logger, task: str) -> bool:
    """Rename ``summary`` → ``description`` in place. Returns True if
    any entry was mutated.
    """
    mutated = False
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        if "description" in entry:
            continue
        if "summary" in entry:
            entry["description"] = entry.pop("summary")
            mutated = True
            log.info(
                "v0_24_3: task '%s' history[%d]: renamed 'summary' → 'description'",
                task, i,
            )
    return mutated


async def upgrade(ctx: MigrationContext) -> None:
    log = ctx.log or DEFAULT_LOG
    continuous_dir = _resolve_continuous_dir(ctx)
    if not continuous_dir.exists():
        log.info("v0_24_3: no continuous dir at %s — nothing to do", continuous_dir)
        return

    touched = 0
    skipped = 0
    for task_dir in sorted(continuous_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        state_path = task_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "v0_24_3: skipping '%s' (unreadable state.json): %s",
                task_dir.name, exc,
            )
            skipped += 1
            continue
        if not isinstance(data, dict):
            skipped += 1
            continue
        history = data.get("history")
        if not isinstance(history, list) or not history:
            continue
        if _normalise_history(history, log, task_dir.name):
            try:
                _atomic_write_json(state_path, data)
                touched += 1
            except OSError as exc:
                log.error(
                    "v0_24_3: failed to write normalised state for '%s': %s",
                    task_dir.name, exc,
                )
                skipped += 1
    log.info(
        "v0_24_3: normalisation complete — %d state files updated, %d skipped",
        touched, skipped,
    )


MIGRATION = Migration(
    from_version="0.24.2",
    to_version="0.24.3",
    description="normalise continuous-task history schema (summary → description)",
    upgrade=upgrade,
)
