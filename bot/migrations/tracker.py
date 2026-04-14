"""Persistence for the migration tracker file (``data/migrations.json``).

The file holds both:

- Legacy name-keyed entries from the pre-0.20.12 framework (each
  previously registered migration under its own string id). These live
  at the root of the JSON object for backwards compatibility with the
  existing :mod:`migrations.legacy` module and its tests.
- A ``_chain_`` object with the new version-chained framework's state:
  ``current_version`` plus a ``history`` list of applied steps.

Old installs only contain the legacy keys; the first boot after
upgrading to 0.20.12 adds the ``_chain_`` section seeded at
``current_version = "0.20.11"`` so the 0.20.12 migration runs next.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("robyx.migrations.tracker")

CHAIN_KEY = "_chain_"
SEED_VERSION = "0.20.11"  # version immediately before the framework landed


def _file(data_dir: Path) -> Path:
    return data_dir / "migrations.json"


def load(data_dir: Path) -> dict[str, Any]:
    """Load the raw tracker dict, returning ``{}`` if absent or corrupt."""
    path = _file(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.warning("Cannot read %s: %s — treating as empty", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("%s has unexpected shape, treating as empty", path)
        return {}
    return data


def save(data_dir: Path, data: dict[str, Any]) -> None:
    path = _file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def get_chain_state(tracker: dict[str, Any]) -> dict[str, Any]:
    """Return (or seed) the ``_chain_`` sub-object inside the tracker.

    Seeding sets ``current_version`` to :data:`SEED_VERSION` — the
    version immediately before the framework landed — so that the
    0.20.12 migration runs next on an existing install.
    """
    chain = tracker.get(CHAIN_KEY)
    if not isinstance(chain, dict):
        chain = {"current_version": SEED_VERSION, "history": []}
        tracker[CHAIN_KEY] = chain
    chain.setdefault("current_version", SEED_VERSION)
    chain.setdefault("history", [])
    return chain


def record_step(
    tracker: dict[str, Any],
    from_version: str,
    to_version: str,
    status: str,
    error: str | None = None,
) -> None:
    """Append an entry to ``_chain_.history`` and advance ``current_version``
    iff the step succeeded."""
    chain = get_chain_state(tracker)
    entry: dict[str, Any] = {
        "from": from_version,
        "to": to_version,
        "status": status,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    if error is not None:
        entry["error"] = error
    chain["history"].append(entry)
    if status == "ok":
        chain["current_version"] = to_version


def current_version(tracker: dict[str, Any]) -> str:
    """Return the tracker's current chain version (seeds if missing)."""
    return get_chain_state(tracker)["current_version"]
