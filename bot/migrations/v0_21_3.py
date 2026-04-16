"""0.21.2 -> 0.21.3 -- media hardening + i18n cleanup + help parity.

Code-only release. Three findings closed plus two safety-net test
additions:

* P2-50 (Med, Security) — ``bot/media.py`` decompression-bomb defence.
  Pre-Pillow 25 MB file-size cap, lowered ``Image.MAX_IMAGE_PIXELS``
  to 50 MP, ``DecompressionBombWarning`` promoted to error.
* P2-01/02/03 (Low, NI) — three hard-coded literals in
  ``bot/handlers.py`` moved to ``bot/i18n.py``'s ``STRINGS`` dict.
* P2-60 (Low, NI) — new parametrised tests in
  ``tests/test_i18n_parity.py`` that catch any future regression in
  ``%s``/``%d`` substitution or ``/help`` ⟷ handler drift.

No on-disk data structure changes. Existing data files continue to
work unchanged.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.21.2",
    to_version="0.21.3",
    description="Media decompression-bomb defence + i18n cleanup + help parity tests",
    upgrade=upgrade,
)
