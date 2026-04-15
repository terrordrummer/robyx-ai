"""0.20.22 -> 0.20.23 -- strip TTS summary blocks from outgoing messages.

Code-only change (new response filter in handlers.py). No data migration.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.22",
    to_version="0.20.23",
    description="Strip [TTS_SUMMARY] blocks from outgoing messages",
    upgrade=upgrade,
)
