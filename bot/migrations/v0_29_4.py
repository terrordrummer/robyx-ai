"""0.29.3 → 0.29.4 — updater smoke-lane hotfix (no-op migration).

The runtime change distinguishes the updater's exact-target import smoke test
from an ordinary boot after an interrupted transaction. Persisted application
state needs no additional rewrite; the migration keeps multi-version update
jumps continuous.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.29.3",
    to_version="0.29.4",
    description="allow only verified updater smoke children past active marker",
    upgrade=upgrade,
)
