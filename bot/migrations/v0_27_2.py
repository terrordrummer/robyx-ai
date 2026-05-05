"""0.27.1 → 0.27.2 — no-op release bump (Telegram typing-indicator continuity fix).

Release 0.27.2 adjusts the chat-action keep-alive loop in
:mod:`bot.handlers` and adds a post-message ``send_typing`` re-assertion
in :mod:`bot.ai_invoke` and the response-chunking loop. The change is
purely in runtime behaviour — no persisted state is read, written, or
restructured. This migration only advances the chain version pointer.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.27.1",
    to_version="0.27.2",
    description="no-op release bump (Telegram typing-indicator continuity fix)",
    upgrade=upgrade,
)
