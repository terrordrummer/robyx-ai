"""0.20.25 -> 0.20.26 -- collaborative workspaces.

New feature, no existing data to migrate.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.25",
    to_version="0.20.26",
    description="Collaborative workspaces: data model, auth, routing, lifecycle commands",
    upgrade=upgrade,
)
