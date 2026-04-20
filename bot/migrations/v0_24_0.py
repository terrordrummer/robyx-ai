"""0.23.0 → 0.24.0 — continuous-task lifecycle hardening.

No state or schema changes. All three improvements in this release are
behavioural:

1. ``checkpoint_policy`` is now injected into the step agent's prompt
   (previously stored in state but never consulted).
2. Workspace agents read ``data/continuous/*/state.json`` at prompt
   assembly time to know which tasks they own.
3. A new ``[UPDATE_PLAN]`` macro edits an existing task's program in
   place.

Existing state files are forward-compatible: ``checkpoint_policy``
already defaulted to ``on-demand`` at both read and write sites, so
pre-0.24.0 tasks behave identically. This migration is a no-op; it
exists only to keep the monotonic version chain required by the
migrations contract.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.23.0",
    to_version="0.24.0",
    description="continuous-task lifecycle hardening (no-op)",
    upgrade=upgrade,
)
