"""0.29.1 → 0.29.2 — universal update bridge (no-op migration).

This patch changes only release compatibility metadata and its regression
contract. It does not alter runtime behaviour or persisted state.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.29.1",
    to_version="0.29.2",
    description="universal update bridge (no-op data migration)",
    upgrade=upgrade,
)
