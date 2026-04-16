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
import threading
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger("robyx.orphans")

_PID_FILE = DATA_DIR / "active-pids.json"
_lock = threading.Lock()


def _load() -> dict[str, dict]:
    try:
        if _PID_FILE.exists():
            data = json.loads(_PID_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", _PID_FILE, exc)
    return {}


def _save(data: dict[str, dict]) -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PID_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, _PID_FILE)


def register(pid: int, *, owner: str = "") -> None:
    """Record that *pid* is a subprocess the bot spawned."""
    if pid <= 0:
        return
    with _lock:
        data = _load()
        data[str(pid)] = {"owner": owner}
        _save(data)


def unregister(pid: int) -> None:
    """Remove *pid* from the registry (the subprocess exited cleanly)."""
    if pid <= 0:
        return
    with _lock:
        data = _load()
        if data.pop(str(pid), None) is not None:
            _save(data)


def cleanup_on_startup() -> list[int]:
    """Force-kill any registered PIDs still alive at boot.

    Returns the list of PIDs that were actually killed (for logging).
    PIDs that no longer exist are simply dropped from the registry.
    """
    from process import get_process_name_sync, is_pid_alive

    with _lock:
        data = _load()
        if not data:
            return []

        killed: list[int] = []
        kept: dict[str, dict] = {}
        for pid_str, meta in data.items():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if not is_pid_alive(pid):
                continue
            name = get_process_name_sync(pid)
            # Only kill processes that look like ours. A recycled PID now
            # pointing at an unrelated process must not be touched.
            if not any(n in name for n in ("claude", "codex", "opencode", "python", "node")):
                log.info(
                    "Orphan cleanup: PID %d recycled as '%s', skipping", pid, name,
                )
                continue
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=5,
                    )
                else:
                    # Prefer signalling the whole process group; fall back
                    # to single-PID if that fails. ``start_new_session=True``
                    # at spawn time gives each CLI its own group.
                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGKILL)
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
                else:
                    killed.append(pid)
                    log.warning(
                        "Orphan cleanup: killed PID %d ('%s', owner=%s)",
                        pid, name, meta.get("owner", "?"),
                    )
            except (OSError, ProcessLookupError) as exc:
                log.info("Orphan cleanup: PID %d already gone: %s", pid, exc)

        # Registry is empty after cleanup — new spawns will repopulate it.
        if data != kept:
            _save(kept)
        return killed
