"""Robyx — startup dependency check.

Runs at the very top of ``bot/bot.py`` before any other ``bot/*`` import.
Its only job is to make sure the virtual environment's packages are in
sync with ``bot/requirements.txt`` — if they are not, it runs
``pip install -r bot/requirements.txt`` once, synchronously, before the
rest of the bot starts.

Why this exists
---------------

When a new release adds a dependency (Pillow in v0.12.0 was the first
real instance), the auto-updater is supposed to reinstall packages as
part of ``apply_update``. If that step fails silently — network blip,
pip returning non-zero, a swallowed exception — the bot reboots on the
new code against the old venv and crashes with ``ImportError: No module
named 'PIL'`` or similar.

This file is the safety net. It runs *every time the bot starts*, but
only performs work when the content of ``requirements.txt`` has actually
changed since the last successful install (tracked via a SHA1 hash
stored inside the venv itself). The common case — same requirements —
is a fast file hash comparison and nothing else.

Design choices
--------------

- **Per-venv marker**: the hash file lives at ``<venv>/.robyx_deps_hash``
  rather than in the project data dir, so different venvs (dev, CI,
  prod) don't share a marker.
- **No third-party imports**: this file can only use the Python stdlib,
  because if a dep is missing we might not even have ``packaging`` etc.
- **Best-effort**: a pip install failure is logged loudly on stderr but
  not fatal — we still let the bot try to import, because the error
  message from the eventual ``ImportError`` is often more informative.
- **Quiet common path**: no output when the hash matches, so the bot
  startup stays clean in operational logs.
- **Skips when no venv**: if there is no ``.venv/`` at the expected path
  we return immediately. This single check covers both dev/manual runs
  and test sessions (pytest runs against the project tree without a
  ``.venv/``), so the bootstrap never shells out to pip while tests
  are executing.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

_BOT_DIR = Path(__file__).parent
_PROJECT_ROOT = _BOT_DIR.parent
_REQUIREMENTS = _BOT_DIR / "requirements.txt"
_VENV_DIR = _PROJECT_ROOT / ".venv"
_DATA_DIR = _PROJECT_ROOT / "data"

# Tokens / API keys stripped from the pip subprocess env, same list as
# bot/updater.py::_CHILD_ENV_SCRUB. Pip doesn't need bot tokens or AI
# provider keys; a malicious setup.py in a transitive dep (or a
# PIP_INDEX_URL-redirected proxy) would otherwise have them in its
# process environment. Pass 2 P2-86 — mirrors P2-71 on the updater.
_CHILD_ENV_SCRUB = frozenset({
    # Platform tokens
    "ROBYX_BOT_TOKEN",
    "KAELOPS_BOT_TOKEN",  # legacy alias
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    # AI provider keys
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
})


def _scrubbed_child_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with platform tokens / AI provider
    keys removed. Used as the ``env=`` argument for the pip subprocess.
    Stdlib-only — this file runs before third-party imports are safe."""
    return {k: v for k, v in os.environ.items() if k not in _CHILD_ENV_SCRUB}


def _compute_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _venv_pip() -> Path | None:
    """Return the venv's pip binary path, or ``None`` if not found."""
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    pip_name = "pip.exe" if sys.platform == "win32" else "pip"
    candidate = _VENV_DIR / bin_dir / pip_name
    return candidate if candidate.exists() else None


def _marker_path() -> Path:
    return _VENV_DIR / ".robyx_deps_hash"


def _log(msg: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print("[robyx bootstrap] %s" % msg, file=stream, flush=True)


def migrate_personal_data_if_needed() -> list[str]:
    """v0.16 safety-net: migrate any leftover repo-root runtime files to ``data/``.

    The authoritative migration happens in ``bot/updater.py`` during
    ``apply_update``, running before ``git pull`` so the source files are
    still in the working tree. This bootstrap mirror runs on **every** bot
    boot and covers the alternative path where the user manually runs
    ``git pull && systemctl restart robyx`` without going through the
    auto-updater. On that path, the pull has already removed the tracked
    repo-root files — but any **untracked** leftovers (e.g. a manually
    created ``agents/zeus-engine.md``) are still present and need to be
    relocated.

    Idempotent: files already present under ``data/`` are never overwritten.
    Uses only the stdlib because it runs before third-party imports.
    """
    moved: list[str] = []
    if not _PROJECT_ROOT.exists():
        return moved

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return moved

    for name in ("tasks.md", "specialists.md"):
        src = _PROJECT_ROOT / name
        dst = _DATA_DIR / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError as e:
                _log("could not migrate %s to data/: %s" % (name, e), err=True)

    for subdir in ("agents", "specialists"):
        src_dir = _PROJECT_ROOT / subdir
        if not src_dir.is_dir():
            continue
        dst_dir = _DATA_DIR / subdir
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for src in sorted(src_dir.glob("*.md")):
            dst = dst_dir / src.name
            if dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
                moved.append("%s/%s" % (subdir, src.name))
            except OSError as e:
                _log(
                    "could not migrate %s/%s to data/: %s" % (subdir, src.name, e),
                    err=True,
                )

    if moved:
        _log("migrated %d file(s) to data/: %s" % (len(moved), ", ".join(moved)))
    return moved


def ensure_dependencies() -> None:
    """Re-run ``pip install`` iff ``requirements.txt`` changed since last success."""
    if not _REQUIREMENTS.exists():
        return
    if not _VENV_DIR.exists():
        # Not running from a venv-managed install (dev mode, manual run).
        return

    current_hash = _compute_hash(_REQUIREMENTS)
    marker = _marker_path()
    if marker.exists():
        try:
            if marker.read_text().strip() == current_hash:
                return
        except OSError:
            pass  # unreadable marker — re-install to be safe

    pip = _venv_pip()
    if pip is None:
        _log("venv pip not found at %s — skipping dep check" % _VENV_DIR, err=True)
        return

    _log("requirements.txt changed — running pip install...")
    try:
        proc = subprocess.run(
            [str(pip), "install", "-r", str(_REQUIREMENTS)],
            cwd=str(_PROJECT_ROOT),
            env=_scrubbed_child_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        _log("pip install timed out after 600s — continuing; imports may fail", err=True)
        return
    except OSError as e:
        _log("pip install could not be launched: %s — continuing; imports may fail" % e, err=True)
        return

    if proc.returncode != 0:
        _log(
            "pip install returned %d — continuing; imports may fail\n"
            "--- stdout (last 1 KB) ---\n%s\n--- stderr (last 1 KB) ---\n%s"
            % (proc.returncode, proc.stdout[-1024:], proc.stderr[-1024:]),
            err=True,
        )
        return

    # Success: persist the hash so we don't re-run next boot.
    try:
        marker.write_text(current_hash)
    except OSError as e:
        _log("could not write marker %s: %s" % (marker, e), err=True)
    _log("dependencies are now in sync (hash=%s)" % current_hash[:12])
