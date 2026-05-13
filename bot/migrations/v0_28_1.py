"""0.28.0 → 0.28.1 — no-op release bump.

Release 0.28.1 is a chat-UX hotfix for the spec 007.1 ``/clear`` command:

- Replaces the silent ``return`` when a non-owner runs ``/clear`` in a
  workspace / specialist topic with an explicit refusal reply.
- Adds a collab-workspace fallback that resolves the target agent via
  ``CollabStore.get_by_chat_id`` when ``msg.thread_id`` is ``None``
  (Telegram non-supergroup, Discord guild without forum topics).
- Tightens the HQ guard so ``platform.is_main_thread`` ``True`` outside
  the configured ``CHAT_ID`` no longer falsely refuses /clear.
- Polishes the success copy: emoji headline + turn count.

All changes are in-process; no persisted state schema changes. This
migration only advances the chain version pointer.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.28.0",
    to_version="0.28.1",
    description="no-op release bump (/clear hotfix: feedback + collab fallback)",
    upgrade=upgrade,
)
