"""0.27.0 → 0.27.1 — no-op release bump (Codex CLI 0.124+ adapter fix).

Release 0.27.1 rewrites :class:`bot.ai_backend.CodexBackend` to drive the
``codex exec`` subcommand introduced in Codex CLI 0.124, replacing the
removed top-level ``-q`` / ``--approval-policy`` / ``--system-prompt``
flags. The change is purely in the command-line shape; no persisted state
is read, written, or restructured. This migration only advances the
chain version pointer.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.27.0",
    to_version="0.27.1",
    description="no-op release bump (Codex CLI 0.124+ adapter fix)",
    upgrade=upgrade,
)
