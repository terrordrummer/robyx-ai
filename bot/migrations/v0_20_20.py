"""0.20.19 → 0.20.20 — workspace-agent continuous tasks + updater ls-remote + README overhaul.

No user-visible data or state changes; all three items are either prompt
template text, code-path optimisation, or documentation.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.19",
    to_version="0.20.20",
    description="workspace-agent continuous tasks + updater ls-remote + README overhaul",
    upgrade=upgrade,
)
