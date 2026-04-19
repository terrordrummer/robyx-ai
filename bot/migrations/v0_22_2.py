"""0.22.1 → 0.22.2 — no-op release bump.

This release has no user-visible data changes; the migration exists purely to keep the version chain continuous.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # TODO: implement migration logic here, or leave as no-op if this
    # release ships no user-visible data / state changes.
    return None


MIGRATION = Migration(
    from_version="0.22.1",
    to_version="0.22.2",
    description="no-op release bump",
    upgrade=upgrade,
)
