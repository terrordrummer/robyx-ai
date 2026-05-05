"""0.26.0 → 0.27.0 — no-op release bump.

Release 0.27.0 adds an optional per-workspace / per-specialist backend
override (``[CREATE_WORKSPACE ... backend="codex"]``) without changing
any persisted schema. ``Agent.from_dict`` filters by known dataclass
fields and unknown keys default to ``None``, so 0.26.x state files load
unchanged on 0.27.0 and inherit the global ``AI_BACKEND`` default. No
state-file rewrite is needed; this migration only advances the chain
version pointer.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.26.0",
    to_version="0.27.0",
    description="no-op release bump (per-agent backend override)",
    upgrade=upgrade,
)
