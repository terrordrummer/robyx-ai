"""0.20.23 -> 0.20.24 -- continuous task recognition in agent prompts.

Prompt-only change. No data migration.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.23",
    to_version="0.20.24",
    description="Continuous task recognition and /loop trigger in agent prompts",
    upgrade=upgrade,
)
