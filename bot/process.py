"""Robyx — Cross-platform process utilities.

Provides process checking that works on macOS, Linux, and Windows
without requiring external dependencies like psutil.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("robyx.process")

# Process names that indicate an AI-related or bot-related process
AI_PROCESS_NAMES = ("claude", "codex", "opencode", "python", "node")


def get_process_identity_sync(pid: int) -> dict[str, object] | None:
    """Return a durable identity for *pid*, or ``None`` if unverifiable.

    A PID and process name are not ownership proofs: both can be reused after a
    Robyx crash.  The start fingerprint is therefore mandatory.  Callers must
    fail safe (retain evidence and never signal) when this function returns
    ``None``.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            script = (
                "$p=Get-Process -Id %d -ErrorAction Stop; "
                "[pscustomobject]@{start=$p.StartTime.ToUniversalTime().Ticks.ToString();"
                "executable=$p.Path;comm=$p.ProcessName;pgid=%d} | "
                "ConvertTo-Json -Compress" % (pid, pid)
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            raw = json.loads(result.stdout)
            start = str(raw.get("start") or "").strip()
            executable = str(raw.get("executable") or "").strip()
            comm = str(raw.get("comm") or "").strip().lower()
            if not start or not executable or not comm:
                return None
            return {
                "start_fingerprint": "windows:%s" % start,
                "executable": executable,
                "comm": comm,
                "pgid": pid,
            }

        proc_root = Path("/proc/%d" % pid)
        if proc_root.exists():
            stat = (proc_root / "stat").read_text()
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2 :].split()
            # ``fields`` starts at procfs field 3; starttime is field 22.
            start_ticks = fields[19]
            boot_id_path = Path("/proc/sys/kernel/random/boot_id")
            boot_id = (
                boot_id_path.read_text().strip()
                if boot_id_path.exists()
                else "unknown-boot"
            )
            executable = os.readlink(proc_root / "exe")
            comm = (proc_root / "comm").read_text().strip().lower()
            pgid = os.getpgid(pid)
            if not boot_id or not start_ticks or not executable or not comm:
                return None
            return {
                "start_fingerprint": "linux:%s:%s" % (boot_id, start_ticks),
                "executable": executable,
                "comm": comm,
                "pgid": pgid,
            }

        start_result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        executable_result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        start = " ".join(start_result.stdout.split())
        executable = executable_result.stdout.strip()
        comm = Path(executable).name.lower()
        pgid = os.getpgid(pid)
        if (
            start_result.returncode != 0
            or executable_result.returncode != 0
            or not start
            or not executable
            or not comm
        ):
            return None
        return {
            "start_fingerprint": "%s:%s" % (sys.platform, start),
            "executable": executable,
            "comm": comm,
            "pgid": pgid,
        }
    except (OSError, IndexError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        log.debug("get_process_identity_sync(%d) failed: %s", pid, exc)
        return None


def process_identity_matches(pid: int, expected: object) -> bool:
    """Return true only for a complete exact match with the live process."""
    if not isinstance(expected, dict):
        return False
    required = {"start_fingerprint", "executable", "comm", "pgid"}
    if not required.issubset(expected) or any(expected[key] in (None, "") for key in required):
        return False
    current = get_process_identity_sync(pid)
    return current is not None and all(current.get(key) == expected.get(key) for key in required)


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists (non-blocking on Linux)."""
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def get_process_name_sync(pid: int) -> str:
    """Synchronous version for startup code (before event loop)."""
    try:
        if sys.platform != "win32":
            comm = Path("/proc/%d/comm" % pid)
            if comm.exists():
                return comm.read_text().strip().lower()
        import subprocess
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                if str(pid) in line:
                    parts = line.split('","')
                    if parts:
                        return parts[0].strip('"').lower()
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip().lower()
    except Exception as e:
        log.debug("get_process_name_sync(%d) failed: %s", pid, e)
    return ""


def is_bot_process_sync(pid: int) -> bool:
    """Synchronous version for startup code (before event loop)."""
    name = get_process_name_sync(pid)
    return "python" in name


async def get_process_name(pid: int) -> str:
    """Get the process name for a given PID without blocking the event loop."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            output = stdout.decode(errors="replace")
            for line in output.strip().splitlines():
                if str(pid) in line:
                    parts = line.split('","')
                    if parts:
                        return parts[0].strip('"').lower()
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "comm=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.decode(errors="replace").strip().lower()
    except (asyncio.TimeoutError, OSError, IndexError) as e:
        log.debug("get_process_name(%d) failed: %s", pid, e)
    return ""


async def is_ai_process(pid: int) -> bool:
    """Check if a PID belongs to an AI-related process (claude, codex, python, etc.)."""
    name = await get_process_name(pid)
    return any(n in name for n in AI_PROCESS_NAMES)
