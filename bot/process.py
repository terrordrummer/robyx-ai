"""Robyx — Cross-platform process utilities.

Provides process checking that works on macOS, Linux, and Windows
without requiring external dependencies like psutil.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("robyx.process")

# Process names that indicate an AI-related or bot-related process
AI_PROCESS_NAMES = ("claude", "codex", "opencode", "python", "node")


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except (subprocess.TimeoutExpired, OSError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def get_process_name(pid: int) -> str:
    """Get the process name for a given PID. Returns empty string on failure."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            # CSV format: "process.exe","PID","Session Name","Session#","Mem Usage"
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
    except (subprocess.TimeoutExpired, OSError, IndexError) as e:
        log.debug("get_process_name(%d) failed: %s", pid, e)
    return ""


def is_bot_process(pid: int) -> bool:
    """Check if a PID belongs to a Python/bot process."""
    name = get_process_name(pid)
    return "python" in name


def is_ai_process(pid: int) -> bool:
    """Check if a PID belongs to an AI-related process (claude, codex, python, etc.)."""
    name = get_process_name(pid)
    return any(n in name for n in AI_PROCESS_NAMES)
