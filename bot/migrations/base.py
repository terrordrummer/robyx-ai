"""Base types for the version-chained migration framework.

Each release of Robyx that ships user-visible data/state changes carries
a matching migration module under ``bot/migrations/vX_Y_Z.py``. A release
with no data changes still ships an empty migration — the chain is
required to be continuous so that multi-version jumps (e.g. 0.20 → 0.25)
always execute each intermediate step in order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

UpgradeFn = Callable[["MigrationContext"], Awaitable[None]]
DowngradeFn = Callable[["MigrationContext"], Awaitable[None]]


@dataclass
class MigrationContext:
    """Everything a migration needs to mutate the runtime.

    ``platform`` and ``manager`` may be ``None`` during offline / test
    runs; migrations that touch them must guard accordingly.
    """

    platform: Any = None
    manager: Any = None
    data_dir: Optional[Path] = None
    log: Any = None


@dataclass
class Migration:
    """One step in the version chain, moving state from *from_version* → *to_version*.

    ``upgrade`` is mandatory; a release with no data changes still ships
    a no-op upgrade function (``async def upgrade(ctx): return``) so the
    chain stays continuous.

    ``downgrade`` is optional — provide it only when a rollback actually
    makes sense for this step.
    """

    from_version: str
    to_version: str
    description: str
    upgrade: UpgradeFn
    downgrade: Optional[DowngradeFn] = None


def version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for comparison.

    Raises ``ValueError`` on malformed input so a typo'd migration
    declaration fails loudly instead of silently sorting wrong.
    """
    parts = version.strip().split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError as e:
        raise ValueError("invalid version string %r: %s" % (version, e)) from e
