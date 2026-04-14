"""0.20.15 → 0.20.16 — no-op (typing-latency fix).

Fill in the rationale and the specific state this step mutates.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # TODO: implement migration logic here, or leave as no-op if this
    # release ships no user-visible data / state changes.
    return None


MIGRATION = Migration(
    from_version="0.20.15",
    to_version="0.20.16",
    description="no-op (typing-latency fix)",
    upgrade=upgrade,
)
