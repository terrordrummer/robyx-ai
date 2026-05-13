"""0.28.1 → 0.28.2 — no-op release bump.

Release 0.28.2 is an auto-updater hotfix:

- ``_safe_stash_pop(strict=True)`` raises :class:`StashPopConflict`
  instead of only logging when ``git stash pop`` leaves unmerged paths.
- ``apply_update`` catches the exception and rolls back the code +
  state snapshot rather than restarting the bot into a file with raw
  conflict markers — the v0.28.0 Linux crashloop.
- Post-stash-pop syntax check (``_check_python_syntax_in_repo``) is a
  belt-and-braces parse pass over every ``.py`` file in ``bot/``;
  failures trigger the same rollback path.

No persisted state schema changes; this migration only advances the
chain version pointer.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.28.1",
    to_version="0.28.2",
    description="no-op release bump (auto-updater stash-pop rollback hotfix)",
    upgrade=upgrade,
)
