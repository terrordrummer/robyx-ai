"""0.24.1 → 0.24.2 — no-op release bump.

Release 0.24.2 fixes the fire-and-forget invariant for continuous tasks
(step agents parking under the on-demand policy would stall the loop,
and the primary-agent resume macro rejected awaiting-input). All
changes are in code paths and prompt templates — no persisted state
schema change, so this migration is a no-op.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.24.1",
    to_version="0.24.2",
    description="no-op release bump",
    upgrade=upgrade,
)
