"""0.20.27 -> 0.20.28 -- review-driven hardening.

Code-only release. New env vars (``CLAIM_TIMEOUT_SECONDS``,
``SMOKE_TEST_TIMEOUT_SECONDS``, ``VOICE_TIMEOUT_SECONDS``) all have
safe defaults; ``REMINDER_MAX_AGE_SECONDS`` default changed from 24 h
to 7 d (values explicitly set in ``.env`` are preserved). No on-disk
data structure changes.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.20.27",
    to_version="0.20.28",
    description="Review-driven hardening: async/process-group/path-allowlist fixes",
    upgrade=upgrade,
)
