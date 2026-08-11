"""0.28.3 → 0.29.0 — repository-hardening release (no-op migration).

Release 0.29.0 adds stricter validators, recovery markers, canonical task
scope, and revisioned continuous-task program state. Each affected loader
performs its own compatible normalisation or fail-closed recovery, so no
eager rewrite of existing runtime data is required. This step advances the
version chain without mutating user state.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.28.3",
    to_version="0.29.0",
    description="repository-hardening release (no-op data migration)",
    upgrade=upgrade,
)
