"""0.24.3 → 0.25.0 — no-op release bump.

Release 0.25.0 is a code-review follow-through: atomicity fix in the
legacy migration tracker, corrupt-JSON auto-recovery in
``CollabStore`` and ``AgentManager`` (drawing from the updater's
``data/`` snapshots), a halt-on-partial-failure tightening of
``v0_23_0``, platform ``parse_mode`` contract clean-up, Discord
message-length handling, a shlex-based migration step parser, and a
handful of smaller bug and documentation fixes. None of these touch
persisted state schemas, so this migration is a no-op.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.24.3",
    to_version="0.25.0",
    description="no-op release bump",
    upgrade=upgrade,
)
