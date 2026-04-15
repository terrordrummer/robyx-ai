"""0.20.24 -> 0.20.25 -- documentation update for agentic loop.

Docs-only change. No data migration.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.24",
    to_version="0.20.25",
    description="Document agentic loop / /loop trigger in architecture and scheduler docs",
    upgrade=upgrade,
)
