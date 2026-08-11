"""0.29.0 → 0.29.1 — CI launcher portability (no-op migration).

This patch changes only the cross-platform process-name smoke assertion. It
does not alter runtime behaviour or persisted state.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.29.0",
    to_version="0.29.1",
    description="CI launcher portability (no-op data migration)",
    upgrade=upgrade,
)
