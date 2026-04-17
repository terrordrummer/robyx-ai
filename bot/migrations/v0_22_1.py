"""0.22.0 → 0.22.1 — fix continuous-task macro leak.

No state or schema changes. The fix is entirely behavioural (response
processor now scrubs the ``[CREATE_CONTINUOUS]`` / ``[CONTINUOUS_PROGRAM]``
macro unconditionally and covers the previously-missing workspace-agent
routing branch). ``data/continuous/<name>/state.json`` is unchanged, so
this migration is a no-op. It exists only to keep the monotonic version
chain required by Principle V.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.22.0",
    to_version="0.22.1",
    description="fix continuous-task macro leak (no-op)",
    upgrade=upgrade,
)
