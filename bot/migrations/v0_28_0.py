"""0.27.2 → 0.28.0 — Discord parity for collaborative workspaces.

Spec 007 extends ``CollabWorkspace`` with three new fields:

- ``platform: str`` — defaults to ``"telegram"`` for pre-007 records.
- ``chat_id: str`` — was ``int`` pre-007; coerced to its string form.
- ``expected_platform: str | None`` — new; defaults to ``None`` (cross-
  platform binding guard for pending workspaces).

This migration normalises the on-disk file
``data/collaborative_workspaces.json`` so every record carries
``platform`` and ``chat_id: str`` in canonical form. In-process,
:meth:`bot.collaborative.CollabWorkspace.from_dict` already tolerates
legacy ``int`` chat_ids and missing ``platform`` keys — the migration
simply persists the normalised shape so future loads do not depend on
on-the-fly coercion.

The migration is a pure data transform on a single JSON file: no
platform side effects, no API calls. Strictly idempotent (re-running on
an already-migrated file produces an identical file). Atomic write via
``temp-file + os.replace`` so a crash mid-rewrite leaves either the old
or new file — never a mixed state.

Contracts: ``specs/007-discord-parity/data-model.md`` §6 and
``specs/007-discord-parity/research.md`` R-MIG-01.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .base import Migration, MigrationContext

DEFAULT_LOG = logging.getLogger("robyx.migrations.v0_28_0")


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_data_dir(ctx: MigrationContext) -> Path:
    if ctx.data_dir is not None:
        return Path(ctx.data_dir)
    from config import DATA_DIR as _DATA_DIR  # type: ignore

    return Path(_DATA_DIR)


def _normalise_record(record: dict) -> tuple[dict, bool]:
    """Apply the spec-007 schema normalisation to a single workspace
    record. Returns ``(new_record, changed)`` where ``changed`` reports
    whether any field was rewritten.

    Idempotent: a record already in canonical form is returned with
    ``changed=False``.
    """
    changed = False
    out = dict(record)

    if "platform" not in out:
        out["platform"] = "telegram"
        changed = True

    raw_chat_id = out.get("chat_id", "0")
    if not isinstance(raw_chat_id, str):
        out["chat_id"] = str(raw_chat_id)
        changed = True
    elif raw_chat_id == "":
        # Defensive: empty string is not a valid chat_id; coerce to "0"
        # so the pending invariant (chat_id == "0" iff pending) holds.
        out["chat_id"] = "0"
        changed = True

    return out, changed


def _atomic_write(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically via temp-file + os.replace.

    Reuses the same primitive used by :class:`CollabStore._write_unlocked`
    so the on-disk shape is byte-equivalent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _write_done_marker(path: Path, log: logging.Logger) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_now_iso_utc() + "\n")
    except OSError as exc:
        log.warning(
            "migration v0_28_0: failed to write done marker at %s: %s",
            path, exc,
        )


async def upgrade(ctx: MigrationContext) -> None:
    """Normalise ``data/collaborative_workspaces.json`` to spec-007
    canonical form. See module docstring for full semantics.
    """
    log = ctx.log or DEFAULT_LOG
    data_dir = _resolve_data_dir(ctx)
    collab_path = data_dir / "collaborative_workspaces.json"
    done_marker = data_dir / "migrations" / "v0_28_0.done"

    if done_marker.exists():
        log.info("migration v0_28_0: done marker present — skipping run")
        return

    if not collab_path.exists():
        log.info(
            "migration v0_28_0: no collaborative_workspaces.json — nothing to migrate",
        )
        _write_done_marker(done_marker, log)
        return

    try:
        raw = collab_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        log.error(
            "migration v0_28_0: cannot read %s: %s — aborting chain",
            collab_path, exc,
        )
        raise

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            "migration v0_28_0: cannot parse %s: %s — aborting chain",
            collab_path, exc,
        )
        raise

    if not isinstance(data, dict):
        log.error(
            "migration v0_28_0: unexpected shape in %s (got %s) — aborting chain",
            collab_path, type(data).__name__,
        )
        raise RuntimeError(
            "collaborative_workspaces.json has unexpected top-level shape",
        )

    rewritten: dict[str, dict] = {}
    total_changed = 0
    added_platform = 0
    coerced_chat_id = 0

    for ws_id, record in data.items():
        if not isinstance(record, dict):
            log.warning(
                "migration v0_28_0: record %s is not a dict — leaving untouched",
                ws_id,
            )
            rewritten[ws_id] = record
            continue
        # Track which specific fields are rewritten (for the post-run summary).
        had_platform = "platform" in record
        had_str_chat_id = isinstance(record.get("chat_id", "0"), str)
        new_record, changed = _normalise_record(record)
        if changed:
            total_changed += 1
            if not had_platform:
                added_platform += 1
            if not had_str_chat_id:
                coerced_chat_id += 1
        rewritten[ws_id] = new_record

    if total_changed:
        _atomic_write(collab_path, rewritten)
        log.info(
            "migration v0_28_0: rewrote %d records (%d added platform, %d coerced chat_id)",
            total_changed, added_platform, coerced_chat_id,
        )
    else:
        log.info(
            "migration v0_28_0: %d records already in canonical form — nothing to write",
            len(rewritten),
        )

    _write_done_marker(done_marker, log)


MIGRATION = Migration(
    from_version="0.27.2",
    to_version="0.28.0",
    description=(
        "collaborative workspaces: add platform field, coerce chat_id to str"
    ),
    upgrade=upgrade,
)
