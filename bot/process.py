"""Robyx — Cross-platform process utilities.

Provides process checking that works on macOS, Linux, and Windows
without requiring external dependencies like psutil.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("robyx.process")

# Process names that indicate an AI-related or bot-related process
AI_PROCESS_NAMES = ("claude", "codex", "opencode", "python", "node")


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


async def is_bot_process(pid: int) -> bool:
    """Check if a PID belongs to a Python/bot process."""
    name = await get_process_name(pid)
    return "python" in name


async def is_ai_process(pid: int) -> bool:
    """Check if a PID belongs to an AI-related process (claude, codex, python, etc.)."""
    name = await get_process_name(pid)
    return any(n in name for n in AI_PROCESS_NAMES)
