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
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from persistence_recovery import guard_json_write, load_json_with_recovery

log = logging.getLogger("robyx.migrations.tracker")

CHAIN_KEY = "_chain_"
SEED_VERSION = "0.20.11"  # version immediately before the framework landed
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


def _file(data_dir: Path) -> Path:
    return data_dir / "migrations.json"


def valid_tracker(value: Any) -> bool:
    """Validate chain progress while tolerating historical legacy values."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return False
    if CHAIN_KEY not in value:
        return True
    chain = value[CHAIN_KEY]
    if not isinstance(chain, dict):
        return False
    current = chain.get("current_version", SEED_VERSION)
    history = chain.get("history", [])
    if (
        not isinstance(current, str)
        or _VERSION_RE.fullmatch(current) is None
        or not isinstance(history, list)
    ):
        return False
    for entry in history:
        if not isinstance(entry, dict):
            return False
        if any(not isinstance(entry.get(key), str) for key in ("from", "to", "status")):
            return False
        if any(
            key in entry and not isinstance(entry[key], str)
            for key in ("from", "to", "status", "applied_at", "error")
        ):
            return False
        for key in ("from", "to"):
            if key in entry and _VERSION_RE.fullmatch(entry[key]) is None:
                return False
    return True


def load(data_dir: Path) -> dict[str, Any]:
    """Load the tracker, failing closed when existing progress is corrupt."""
    path = _file(data_dir)
    result = load_json_with_recovery(
        path,
        data_dir=data_dir,
        validator=valid_tracker,
        kind="migration tracker",
        logger=log,
    )
    if result.status == "missing":
        return {}
    return result.value


def save(data_dir: Path, data: dict[str, Any]) -> None:
    """Persist the tracker atomically.

    The write goes through ``tmp + fsync + os.replace`` so a SIGKILL or
    power loss mid-save can never leave ``migrations.json`` partially
    written. A corrupt tracker would be treated as empty on next boot
    and every migration in the chain would re-run — safe for idempotent
    steps, dangerous for ones that assume they've already run once.
    Closes Pass 2 C2 (crash-matrix.md).
    """
    path = _file(data_dir)
    if not valid_tracker(data):
        raise ValueError("refusing to persist malformed migration tracker")
    guard_json_write(
        path,
        data_dir=data_dir,
        validator=valid_tracker,
        kind="migration tracker",
        logger=log,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2)
    # Write + fsync the tmp file so its bytes are durable on disk before
    # we swing the directory entry.
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # fsync may fail on filesystems that don't support it
            # (tmpfs, some network mounts) — fall back to the rename
            # which is still atomic, just not durable against a host
            # power loss on those filesystems.
            pass
    os.replace(tmp, path)


def get_chain_state(tracker: dict[str, Any]) -> dict[str, Any]:
    """Return (or seed) the ``_chain_`` sub-object inside the tracker.

    Seeding sets ``current_version`` to :data:`SEED_VERSION` — the
    version immediately before the framework landed — so that the
    0.20.12 migration runs next on an existing install.
    """
    if CHAIN_KEY not in tracker:
        chain = {"current_version": SEED_VERSION, "history": []}
        tracker[CHAIN_KEY] = chain
    else:
        chain = tracker[CHAIN_KEY]
        if not isinstance(chain, dict):
            raise ValueError("malformed migration chain state")
    chain.setdefault("current_version", SEED_VERSION)
    chain.setdefault("history", [])
    if not isinstance(chain["current_version"], str) or not isinstance(chain["history"], list):
        raise ValueError("malformed migration chain progress")
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
