"""0.21.0 -> 0.21.1 -- Pass 2 security + stability slice.

Code-only release. Three findings closed:

* P2-11 (Med, Security) — ``discord.py`` voice download streams with a
  25 MB cap; unbounded ``resp.read()`` removed.
* P2-12 (Med, Security) — Discord HTTPS + hostname allow-list
  generalised into ``_validate_discord_url`` and applied to every
  download path; ``discordapp.net`` added to the allow-list.
* P2-30 (Med, Stability) — corrupt ``state.json`` or
  ``collaborative_workspaces.json`` is now renamed to
  ``*.corrupt-<UTC-timestamp>`` on load failure before the bot falls
  back to empty state. Closes the long-standing risk that the next
  write silently overwrites the original bytes (Pass 1 F17, deferred
  since v0.20.28).

No on-disk data structure changes. Existing ``state.json`` and
``collaborative_workspaces.json`` files continue to work unchanged; the
quarantine path only fires on load failure.
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    return None


MIGRATION = Migration(
    from_version="0.21.0",
    to_version="0.21.1",
    description="Pass 2 security+stability: Discord DL hardening + corrupt-file quarantine",
    upgrade=upgrade,
)
