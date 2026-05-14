"""0.28.2 → 0.28.3 — rate-limit false-positive fix (no-op data migration).

The fix is entirely in bot/ai_invoke.py classification logic; no
persisted state schema changes.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.28.2",
    to_version="0.28.3",
    description="rate-limit false-positive fix (no-op data migration)",
    upgrade=upgrade,
)
