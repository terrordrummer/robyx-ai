"""0.21.1 -> 0.21.2 -- migration tracker atomicity.

Code-only release. One finding closed:

* P2-40 (Med, Stability) — ``bot/migrations/tracker.py`` ``save()``
  now writes through ``tmp + fsync + os.replace`` so a SIGKILL or
  power loss mid-write can no longer corrupt ``data/migrations.json``.
  Previously a partial write would make ``load()`` treat the file as
  empty on next boot and re-run every migration in the chain — safe
  only if every step is strictly idempotent. Closes C2 from
  ``specs/002-full-code-review/crash-matrix.md``.

No on-disk data structure changes. Existing ``migrations.json``
files continue to work unchanged; only the write path changed.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.21.1",
    to_version="0.21.2",
    description="Migration tracker: atomic save (tmp + fsync + os.replace)",
    upgrade=upgrade,
)
