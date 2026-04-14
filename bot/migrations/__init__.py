"""Robyx migration framework.

Two layers, both tracked in ``data/migrations.json``:

1. **Legacy name-keyed registry** (``.legacy``) — pre-0.20.12 migrations
   registered via ``@migration(id=..., description=...)``. Kept for
   backwards compatibility with existing installs; new migrations should
   *not* be added here.

2. **Version-chained framework** (``.runner`` + ``vX_Y_Z.py``) — every
   release ships a matching migration module, even if it is a no-op.
   The chain must be continuous: each step's ``from_version`` equals the
   previous step's ``to_version``. Multi-version jumps run every
   intermediate step in order, so state is never skipped.

Public entry point: :func:`run_pending` (same signature as before —
``(platform, manager) -> list[tuple[str, str]]``). Internally it runs
legacy migrations first, then the version chain.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import legacy
from .base import Migration, MigrationContext, version_tuple
from .legacy import (
    LegacyMigrationEntry,
    MIGRATIONS_FILE,
    _REGISTRY,
    _load_applied,
    _migrate_kaelops_to_robyx,
    _rename_to_command_bridge,
    _rename_to_headquarters,
    _reset_sessions_after_clobber_fix,
    _reset_sessions_for_reminder_skill,
    _save_applied,
    clear_registry_for_tests,
    migration,
)
from .legacy import run_pending as _run_legacy_pending
from .runner import discover, run_chain, slice_pending, validate_chain
from .tracker import current_version as _tracker_current_version
from .tracker import load as _tracker_load

log = logging.getLogger("robyx.migrations")


# Import every vX_Y_Z module so their MIGRATION constants register
# themselves with :func:`runner.discover`. Side-effect imports are
# intentional — keeping the list explicit makes the contract test job
# trivial (it just compares this list against the releases/ directory).
from . import v0_20_12  # noqa: F401


async def run_pending(platform, manager) -> list[tuple[str, str]]:
    """Unified entry point: legacy name-keyed migrations, then version chain.

    Returns a list of ``(label, status)`` tuples covering both layers,
    in the order they were executed. Legacy entries use their historical
    id; chain entries use ``"<from>→<to>"``.
    """
    legacy_results = await _run_legacy_pending(platform, manager)

    # Derive the data dir from the legacy tracker path at *call* time so
    # tests that monkeypatch ``legacy.MIGRATIONS_FILE`` redirect both the
    # legacy and the chain tracker to the same tmp location.
    data_dir = Path(legacy.MIGRATIONS_FILE).parent
    ctx = MigrationContext(
        platform=platform,
        manager=manager,
        data_dir=data_dir,
        log=log,
    )
    chain_results = await run_chain(ctx, data_dir, package_name=__name__)

    return list(legacy_results) + list(chain_results)


__all__ = [
    # Legacy back-compat surface
    "LegacyMigrationEntry",
    "MIGRATIONS_FILE",
    "_REGISTRY",
    "_load_applied",
    "_save_applied",
    "clear_registry_for_tests",
    "migration",
    # New framework surface
    "Migration",
    "MigrationContext",
    "version_tuple",
    "discover",
    "run_chain",
    "slice_pending",
    "validate_chain",
    # Unified entry point
    "run_pending",
]
