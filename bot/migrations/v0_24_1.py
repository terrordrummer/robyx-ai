"""0.24.0 → 0.24.1 — no-op release bump.

Fill in the rationale and the specific state this step mutates.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # TODO: implement migration logic here, or leave as no-op if this
    # release ships no user-visible data / state changes.
    return None


MIGRATION = Migration(
    from_version="0.24.0",
    to_version="0.24.1",
    description="no-op release bump",
    upgrade=upgrade,
)
