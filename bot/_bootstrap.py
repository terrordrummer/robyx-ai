"""Robyx — startup dependency check.

Runs at the very top of ``bot/bot.py`` before any other ``bot/*`` import.
Its only job is to make sure the virtual environment's packages are in sync
with the committed runtime lock for this Python minor. If they are not, it
runs a hash-verified install once, synchronously, before the bot starts.

Why this exists
---------------

When a new release adds a dependency (Pillow in v0.12.0 was the first
real instance), the auto-updater is supposed to reinstall packages as
part of ``apply_update``. If that step fails silently — network blip,
pip returning non-zero, a swallowed exception — the bot reboots on the
new code against the old venv and crashes with ``ImportError: No module
named 'PIL'`` or similar.

This file is the safety net. It runs *every time the bot starts*, but
only performs work when the input requirements or selected runtime lock have
changed since the last successful install (tracked via a SHA256 fingerprint
stored inside the venv itself). The common case is a fast comparison.

Design choices
--------------

- **Per-venv marker**: the hash file lives at ``<venv>/.robyx_deps_hash``
  rather than in the project data dir, so different venvs (dev, CI,
  prod) don't share a marker.
- **No third-party imports**: this file can only use the Python stdlib,
  because if a dep is missing we might not even have ``packaging`` etc.
- **Fail closed**: a missing lock or failed install stops boot with a concise
  dependency error. New code must never continue against a stale environment.
- **Quiet common path**: no output when the hash matches, so the bot
  startup stays clean in operational logs.
- **Runs only inside the managed venv**: ``.venv/`` must both exist and be the
  running interpreter's ``sys.prefix``. A developer, CI, or test interpreter
  must never mutate a different checkout-local environment merely because it
  happens to exist.
"""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from dependency_locks import (
    DependencyLockError,
    dependency_fingerprint,
    dependency_lock_path,
)

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
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)",
    re.IGNORECASE,
)
_PIP_TRANSPORT_ENV = frozenset({
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
})


def _scrubbed_child_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with platform tokens / AI provider
    keys removed. Used as the ``env=`` argument for the pip subprocess.
    Stdlib-only — this file runs before third-party imports are safe."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _CHILD_ENV_SCRUB
        and not (
            _SENSITIVE_ENV_NAME.search(key)
            and key not in _PIP_TRANSPORT_ENV
        )
    }


def _runtime_lock() -> Path:
    return dependency_lock_path(_PROJECT_ROOT, kind="runtime")


def _current_dependency_hash(lock_path: Path) -> str:
    return dependency_fingerprint(_REQUIREMENTS, lock_path)


def _venv_pip() -> Path | None:
    """Return the venv's pip binary path, or ``None`` if not found."""
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    pip_name = "pip.exe" if sys.platform == "win32" else "pip"
    candidate = _VENV_DIR / bin_dir / pip_name
    return candidate if candidate.exists() else None


def _marker_path() -> Path:
    return _VENV_DIR / ".robyx_deps_hash"


def _running_from_managed_venv() -> bool:
    """Return whether this interpreter owns the checkout-local runtime venv."""
    try:
        return Path(sys.prefix).resolve() == _VENV_DIR.resolve()
    except OSError:
        return False


def _log(msg: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print("[robyx bootstrap] %s" % msg, file=stream, flush=True)


def _terminate_pip_process(proc: subprocess.Popen) -> None:
    """Terminate the hash-install process group and reap its leader."""
    if proc.poll() is not None:
        proc.wait()
        return
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    else:  # pragma: no cover - no Windows CI
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.terminate()
    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    else:  # pragma: no cover
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    proc.wait()


def _run_pip_install(
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run pip in an isolated group with cleanup on timeout or parent unwind."""
    previous_handlers: dict[int, object] = {}

    def _bootstrap_signal(signum, _frame):
        raise SystemExit(128 + int(signum))

    # This runs before bot.main() installs the normal shutdown hooks. Without
    # a temporary handler, SIGTERM would kill the Python parent immediately
    # while isolated pip/build children continued mutating .venv.
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _bootstrap_signal)

    proc = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=sys.platform != "win32",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32"
                else 0
            ),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        # Ignore repeated termination signals for the bounded cleanup window;
        # original handlers are restored in finally before propagation.
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        if proc is not None:
            _terminate_pip_process(proc)
        raise
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _write_marker_atomically(marker: Path, value: str) -> None:
    """Commit a successful dependency fingerprint without partial markers."""
    fd, temp_name = tempfile.mkstemp(
        prefix=".%s." % marker.name,
        suffix=".tmp",
        dir=str(marker.parent),
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _active_update_smoke_matches_target(payload: dict) -> bool:
    """Return whether a controlled smoke child matches its durable target.

    The updater intentionally keeps ``active-update.json`` in place while it
    imports the freshly installed runtime. That child is safe to admit only
    for the ``smoke-test`` phase and only when both Git HEAD and ``VERSION``
    match the exact target recorded before checkout. Ordinary service boots
    never use this exception.
    """
    target_commit = payload.get("target_commit")
    target_version = payload.get("target_version")
    if not (
        payload.get("phase") == "smoke-test"
        and isinstance(target_commit, str)
        and bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", target_commit))
        and isinstance(target_version, str)
        and bool(target_version.strip())
    ):
        return False

    try:
        installed_version = (_PROJECT_ROOT / "VERSION").read_text(
            encoding="utf-8",
        ).strip()
        git_env = _scrubbed_child_env()
        for key in tuple(git_env):
            if key.startswith("GIT_"):
                git_env.pop(key, None)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_PROJECT_ROOT),
            env=git_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return False
    return (
        head.returncode == 0
        and head.stdout.strip().lower() == target_commit.lower()
        and installed_version == target_version.strip()
    )


def _assert_no_interrupted_update(*, allow_target_smoke: bool = False) -> bool:
    """Fail boot closed when a prior updater died after changing code/data.

    Returns ``True`` only for the updater-owned, exact-target smoke-test lane.
    The caller uses that signal to prohibit an opportunistic dependency
    install: dependencies must already have the fingerprint committed by the
    parent update transaction.
    """
    marker = _DATA_DIR / "backups" / "active-update.json"
    if not marker.exists():
        return False
    valid = False
    phase = "unknown"
    pre_commit = ""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        phase = str(payload.get("phase", "unknown"))
        pre_commit = str(payload.get("pre_update_commit", ""))
        valid = (
            isinstance(payload, dict)
            and payload.get("schema") == 1
            and phase in {
                "stash",
                "workspace-prepare",
                "checkout",
                "migration",
                "dependencies",
                "smoke-test",
            }
            and bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", pre_commit))
            and isinstance(payload.get("pre_update_version"), str)
            and bool(payload.get("pre_update_version"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        valid = False

    if allow_target_smoke and valid and _active_update_smoke_matches_target(payload):
        return True

    detail = "phase=%s pre-update=%s" % (
        phase,
        pre_commit[:12] if valid else "unverified",
    )
    raise DependencyLockError(
        "interrupted update detected (%s); refusing to boot unverified code. "
        "Inspect %s, restore the recorded commit/data snapshot, and remove "
        "the marker only after smoke-testing the recovered runtime."
        % (detail, marker)
    )


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


def ensure_dependencies(*, allow_interrupted_update_smoke: bool = False) -> None:
    """Install the selected runtime lock iff its fingerprint changed."""
    active_update_smoke = _assert_no_interrupted_update(
        allow_target_smoke=allow_interrupted_update_smoke,
    )
    if not _REQUIREMENTS.exists():
        return
    if not _VENV_DIR.exists() or not _running_from_managed_venv():
        # Dev/CI may execute this checkout from another virtual environment.
        # Never install into a distinct checkout-local .venv in that case.
        return

    try:
        lock_path = _runtime_lock()
        current_hash = _current_dependency_hash(lock_path)
    except (DependencyLockError, OSError) as exc:
        message = "dependency lock unavailable — refusing unlocked boot: %s" % exc
        _log(message, err=True)
        raise DependencyLockError(message) from exc
    marker = _marker_path()
    if marker.exists():
        try:
            if marker.read_text().strip() == current_hash:
                return
        except OSError:
            pass  # unreadable marker — re-install to be safe

    if active_update_smoke:
        raise DependencyLockError(
            "active-update smoke test found an unverified dependency "
            "fingerprint; refusing to install outside the parent transaction"
        )

    pip = _venv_pip()
    if pip is None:
        message = "venv pip not found at %s — refusing stale dependency boot" % _VENV_DIR
        _log(message, err=True)
        raise DependencyLockError(message)

    _log("dependency lock changed — running hash-verified pip install...")
    try:
        proc = _run_pip_install(
            [
                str(pip),
                "install",
                "--require-hashes",
                "-r",
                str(lock_path),
            ],
            timeout=600,
            env=_scrubbed_child_env(),
        )
    except subprocess.TimeoutExpired:
        message = "hash-verified pip install timed out after 600s"
        _log(message, err=True)
        raise DependencyLockError(message) from None
    except OSError as e:
        message = "hash-verified pip install could not be launched: %s" % e
        _log(message, err=True)
        raise DependencyLockError(message) from e

    if proc.returncode != 0:
        # pip output can echo authenticated index URLs and arbitrary setup.py
        # diagnostics. Preserve the exit status, never the raw child streams.
        _log(
            "hash-verified pip install failed with exit status %d"
            % proc.returncode,
            err=True,
        )
        raise DependencyLockError(
            "hash-verified pip install failed with exit status %d" % proc.returncode
        )

    # Success: persist the hash so we don't re-run next boot.
    try:
        _write_marker_atomically(marker, current_hash)
    except OSError as e:
        _log("could not write marker %s: %s" % (marker, e), err=True)
    _log("dependencies are now in sync (hash=%s)" % current_hash[:12])
