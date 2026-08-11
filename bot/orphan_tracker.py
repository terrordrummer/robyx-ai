"""Robyx — Orphan subprocess PID tracker.

The bot can crash or be killed while an AI subprocess is still running (e.g.
during ``agent.interrupt()`` between the SIGTERM and the process confirming
death). Those PIDs survive the bot and keep consuming resources.

This module maintains ``data/active-pids.json`` — a small registry of
PIDs the bot believes are currently running on its behalf. Entries are
added when a subprocess is spawned and removed when it exits cleanly.
At startup the bot reads the file and force-kills any remaining entries
that are still alive and look like AI processes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

from config import DATA_DIR
from persistence_recovery import (
    PersistenceUnavailableError,
    guard_json_write,
    load_json_with_recovery,
)

log = logging.getLogger("robyx.orphans")

_PID_FILE = DATA_DIR / "active-pids.json"
_lock = threading.Lock()


def _valid_registry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for pid, meta in value.items():
        if not isinstance(pid, str) or not isinstance(meta, dict):
            return False
        owner = meta.get("owner")
        if owner is not None and not isinstance(owner, str):
            return False
        identity = meta.get("identity")
        if identity is not None:
            if not isinstance(identity, dict):
                return False
            if not {
                "start_fingerprint", "executable", "comm", "pgid",
            }.issubset(identity):
                return False
    return True


def _load() -> dict[str, dict]:
    result = load_json_with_recovery(
        _PID_FILE,
        data_dir=_PID_FILE.parent,
        validator=_valid_registry,
        kind="orphan PID registry",
        logger=log,
    )
    if result.status == "missing":
        return {}
    return result.value


def _save(data: dict[str, dict]) -> None:
    if not _valid_registry(data):
        raise ValueError("refusing to persist malformed orphan PID registry")
    guard_json_write(
        _PID_FILE,
        data_dir=_PID_FILE.parent,
        validator=_valid_registry,
        kind="orphan PID registry",
        logger=log,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=_PID_FILE.name + ".",
        suffix=".tmp",
        dir=_PID_FILE.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, _PID_FILE)
    finally:
        # Another process may be maintaining the same registry during an
        # upgrade/test handoff.  A unique temporary path avoids rename races;
        # clean it up if the durable replace did not happen.
        tmp.unlink(missing_ok=True)


def register(pid: int, *, owner: str = "") -> None:
    """Record that *pid* is a subprocess the bot spawned."""
    if pid <= 0:
        return
    with _lock:
        from process import get_process_identity_sync

        data = _load()
        data[str(pid)] = {
            "owner": owner,
            "identity": get_process_identity_sync(pid),
        }
        _save(data)


def unregister(pid: int) -> None:
    """Remove *pid* from the registry (the subprocess exited cleanly)."""
    if pid <= 0:
        return
    with _lock:
        data = _load()
        if data.pop(str(pid), None) is not None:
            _save(data)


def registered_identity_matches(pid: int) -> bool:
    """Verify that the live *pid* is the exact process Robyx registered.

    Legacy owner-only records deliberately return ``False``.  They remain
    useful forensic evidence, but are not sufficient authority to signal a
    possibly reused PID.
    """
    from process import process_identity_matches

    with _lock:
        meta = _load().get(str(pid))
    return bool(meta and process_identity_matches(pid, meta.get("identity")))


def cleanup_on_startup() -> list[int]:
    """Force-kill any registered PIDs still alive at boot.

    Returns the list of PIDs that were actually killed (for logging).
    PIDs that no longer exist are simply dropped from the registry.
    """
    from process import get_process_identity_sync, is_pid_alive, process_identity_matches

    with _lock:
        try:
            data = _load()
        except PersistenceUnavailableError as exc:
            # ``bot.main`` historically catches ordinary cleanup errors.  A
            # lost PID registry is different: proceeding could abandon live
            # children, so make this a startup-fatal condition.
            raise SystemExit("orphan PID registry unavailable: %s" % exc) from exc
        if not data:
            return []

        killed: list[int] = []
        kept: dict[str, dict] = {}
        for pid_str, meta in data.items():
            try:
                pid = int(pid_str)
            except ValueError:
                kept[pid_str] = meta
                log.warning("Orphan cleanup: preserving malformed PID evidence %r", pid_str)
                continue
            if not is_pid_alive(pid):
                continue
            expected_identity = meta.get("identity")
            current_identity = get_process_identity_sync(pid)
            if current_identity is None or not process_identity_matches(
                pid,
                expected_identity,
            ):
                log.warning(
                    "Orphan cleanup: PID %d identity is missing or changed; "
                    "preserving evidence without signalling",
                    pid,
                )
                kept[pid_str] = meta
                continue
            name = str(current_identity.get("comm") or "unknown")
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=5,
                    )
                else:
                    # Prefer signalling the whole process group; fall back
                    # to single-PID if that fails. ``start_new_session=True``
                    # at spawn time gives each CLI its own group.
                    try:
                        pgid = int(current_identity["pgid"])
                        if pgid == pid:
                            os.killpg(pgid, signal.SIGKILL)
                        else:
                            # A registry may survive an upgrade from a version
                            # that did not isolate children. Never signal a shared
                            # process group: it can contain the service manager or
                            # another unrelated process. Kill only the verified PID.
                            log.warning(
                                "Orphan cleanup: PID %d is not process-group leader "
                                "(pgid=%d); using leader-only fallback",
                                pid,
                                pgid,
                            )
                            os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        os.kill(pid, signal.SIGKILL)
                # Verify the process actually died — SELinux or unusual
                # permissions can silently cause SIGKILL to fail.
                import time as _time
                _time.sleep(0.1)
                if is_pid_alive(pid):
                    log.warning(
                        "Orphan cleanup: SIGKILL sent to PID %d but it is "
                        "still alive — giving up", pid,
                    )
                    kept[pid_str] = meta
                else:
                    killed.append(pid)
                    log.warning(
                        "Orphan cleanup: killed PID %d ('%s', owner=%s)",
                        pid, name, meta.get("owner", "?"),
                    )
            except (OSError, ProcessLookupError) as exc:
                log.info("Orphan cleanup: PID %d already gone: %s", pid, exc)
                if is_pid_alive(pid):
                    kept[pid_str] = meta

        # Registry is empty after cleanup — new spawns will repopulate it.
        if data != kept:
            _save(kept)
        return killed
