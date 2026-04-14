"""0.20.21 → 0.20.22 — rollback keeps HEAD attached + branch check before pull.

No user-visible data or state changes; updater code-path hardening.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.21",
    to_version="0.20.22",
    description="rollback via reset --hard + pre-pull branch==main check",
    upgrade=upgrade,
)
