"""0.20.20 → 0.20.21 — updater hotfix (smoke test + detached HEAD).

No user-visible data or state changes; both items are updater code-path
fixes. The smoke test invocation changes from ``python -c "import
bot.bot"`` to ``python bot/bot.py --smoke-test``; the pull step now
re-attaches to ``main`` before pulling when HEAD is detached.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.20",
    to_version="0.20.21",
    description="updater hotfix: smoke test via bot.py --smoke-test + detached-HEAD recovery",
    upgrade=upgrade,
)
