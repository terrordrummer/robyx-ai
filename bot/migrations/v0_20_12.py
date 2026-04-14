"""0.20.11 → 0.20.12 — bootstrap the version-chained migration framework.

This release introduces the framework itself; there is no user-visible
data to migrate. The migration is a no-op, whose existence is the
point — the tracker advances from 0.20.11 to 0.20.12, and every future
release ships its own ``v0_X_Y.py`` step so multi-version jumps stay
safe.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # Nothing to migrate — the framework itself is the only change.
    return None


MIGRATION = Migration(
    from_version="0.20.11",
    to_version="0.20.12",
    description="Bootstrap the version-chained migration framework",
    upgrade=upgrade,
)
