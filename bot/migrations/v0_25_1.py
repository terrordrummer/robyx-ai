"""0.25.0 → 0.25.1 — no-op release bump.

Release 0.25.1 adds two defenses to the auto-updater: a pre-flight
gate that refuses to run when the repo has unmerged index entries or
an in-progress merge/rebase/cherry-pick, and louder logging when the
post-update ``git stash pop`` leaves conflict markers behind.
Closes a field incident where a prior silent stash-pop conflict had
wedged auto-update on a deployment. No persisted state schema
changes; this migration is a no-op.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.25.0",
    to_version="0.25.1",
    description="no-op release bump",
    upgrade=upgrade,
)
