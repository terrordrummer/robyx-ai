"""0.20.12 → 0.20.13 — no-op (test stabilization release).

Fill in the rationale and the specific state this step mutates.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # TODO: implement migration logic here, or leave as no-op if this
    # release ships no user-visible data / state changes.
    return None


MIGRATION = Migration(
    from_version="0.20.12",
    to_version="0.20.13",
    description="no-op (test stabilization release)",
    upgrade=upgrade,
)
