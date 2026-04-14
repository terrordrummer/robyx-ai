"""Version-chain discovery and execution.

The runner walks ``bot/migrations/v*.py``, validates that every module
exposes a ``MIGRATION`` constant of type :class:`Migration`, and that
the ``from_version`` / ``to_version`` fields form a single continuous
chain (no gaps, no duplicates, no forks).

Execution runs every migration whose ``from_version`` ≥ the tracker's
``current_version``, in chain order, up to the latest available step —
or until one step fails, in which case the chain stops and the failure
is surfaced to the caller.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from pathlib import Path
from typing import Any

from .base import Migration, MigrationContext, version_tuple
from .tracker import current_version as tracker_current_version
from .tracker import load, record_step, save

log = logging.getLogger("robyx.migrations.runner")

_MODULE_PATTERN = re.compile(r"^v\d+_\d+_\d+$")


def discover(package_name: str = "migrations") -> list[Migration]:
    """Import every ``vX_Y_Z`` module in the package and collect their
    ``MIGRATION`` constants.

    Modules that are present but lack a well-formed ``MIGRATION`` are
    flagged with a loud warning — this should be impossible in a
    released build because the contract test fails first, but the guard
    protects against local hacks.
    """
    package = importlib.import_module(package_name)
    migrations: list[Migration] = []
    pkg_path = Path(package.__file__).parent  # type: ignore[arg-type]

    for info in pkgutil.iter_modules([str(pkg_path)]):
        if not _MODULE_PATTERN.match(info.name):
            continue
        module = importlib.import_module("%s.%s" % (package_name, info.name))
        mig = getattr(module, "MIGRATION", None)
        if not isinstance(mig, Migration):
            log.warning(
                "Migration module %s.%s has no MIGRATION constant — skipping",
                package_name, info.name,
            )
            continue
        migrations.append(mig)

    migrations.sort(key=lambda m: version_tuple(m.to_version))
    return migrations


def validate_chain(migrations: list[Migration]) -> None:
    """Raise ``ValueError`` if the chain is not a single continuous path.

    Each migration's ``to_version`` must equal the next migration's
    ``from_version``. Duplicates, forks, or gaps are all errors.
    """
    seen_to: set[str] = set()
    for i, m in enumerate(migrations):
        if m.to_version in seen_to:
            raise ValueError(
                "duplicate migration to_version=%s" % m.to_version
            )
        seen_to.add(m.to_version)
        if i == 0:
            continue
        prev = migrations[i - 1]
        if m.from_version != prev.to_version:
            raise ValueError(
                "chain gap: %s → %s followed by %s → %s" % (
                    prev.from_version, prev.to_version,
                    m.from_version, m.to_version,
                )
            )


def slice_pending(
    migrations: list[Migration],
    current: str,
) -> list[Migration]:
    """Return the sub-sequence of migrations that must still run.

    A migration runs iff its ``to_version`` is strictly greater than
    *current* (i.e. the tracker hasn't reached it yet).
    """
    cur = version_tuple(current)
    return [m for m in migrations if version_tuple(m.to_version) > cur]


async def run_chain(
    ctx: MigrationContext,
    data_dir: Path,
    package_name: str = "migrations",
) -> list[tuple[str, str]]:
    """Run every pending migration in the chain and return a summary.

    The summary is a list of ``("<from>→<to>", status)`` tuples, where
    ``status`` is ``"ok"`` for successful steps, ``"error"`` for steps
    that raised. On the first ``error`` the chain stops immediately and
    the remaining steps stay pending — they will re-attempt on the next
    boot.
    """
    migrations = discover(package_name)
    validate_chain(migrations)

    tracker = load(data_dir)
    current = tracker_current_version(tracker)
    pending = slice_pending(migrations, current)

    if not pending:
        log.debug("Migration chain up to date at %s", current)
        return []

    summary: list[tuple[str, str]] = []
    for m in pending:
        label = "%s→%s" % (m.from_version, m.to_version)
        log.info("Running migration %s: %s", label, m.description)
        try:
            await m.upgrade(ctx)
            record_step(tracker, m.from_version, m.to_version, "ok")
            save(data_dir, tracker)
            summary.append((label, "ok"))
            log.info("Migration %s: done", label)
        except Exception as e:
            log.error("Migration %s raised: %s", label, e, exc_info=True)
            record_step(tracker, m.from_version, m.to_version, "error", str(e))
            save(data_dir, tracker)
            summary.append((label, "error"))
            # Stop the chain — subsequent migrations may assume this one ran.
            break

    return summary
