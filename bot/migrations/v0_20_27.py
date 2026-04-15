"""0.20.26 -> 0.20.27 -- silent scheduled delivery + collaborative hardening.

Code-only release. ``expected_creator_id`` is a new optional field on
``CollabWorkspace``; absent values default to ``None`` at load time, so
existing ``collaborative_workspaces.json`` files parse without change.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.26",
    to_version="0.20.27",
    description="Silent scheduled delivery + collaborative workspaces hardening",
    upgrade=upgrade,
)
