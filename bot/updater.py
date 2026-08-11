"""Robyx — Auto-update system.

Checks for new releases (git tags), notifies the owner once per version,
and applies updates with rollback on failure.

Release notes live in releases/<version>.md with YAML frontmatter:
  version, min_compatible, breaking, requires_migration
Migration steps are shell commands listed under a ## Migration heading.
"""

import asyncio
import json
import logging
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import Version

from config import (
    DATA_DIR,
    PROJECT_ROOT,
    UPDATES_STATE_FILE,
    VERSION_FILE,
)
from dependency_locks import (
    DependencyLockError,
    dependency_fingerprint,
    dependency_lock_path,
)
from maintenance import MaintenanceBusyError, get_maintenance_gate
from runtime_supervisor import get_runtime_supervisor
from session_lifecycle import invalidate_sessions_via_manager

log = logging.getLogger("robyx.updater")


# ── Version helpers ──


def get_current_version() -> str:
    """Read current version from VERSION file."""
    return VERSION_FILE.read_text().strip()


def _load_state() -> dict:
    """Load update state from disk."""
    if UPDATES_STATE_FILE.exists():
        try:
            return json.loads(UPDATES_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "notified_versions": [],
        "last_check": None,
        "last_update": None,
        "update_history": [],
    }


def _save_state(state: dict):
    """Persist update state to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPDATES_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ── Child-process env hygiene (Pass 2 T067 / P2-71) ─────────────────
# pip and migration steps are spawned from updater.apply_update. Neither
# needs any platform tokens or AI provider keys — scrubbing them closes
# a hostile-setup.py / malicious-PIP_INDEX_URL class of attacks where a
# transitive dep could read our secrets from the inherited env. Mirrors
# the pattern T066 added to bot/ai_invoke.py, with a broader scrub list
# (pip/migrations don't need AI keys either; the AI CLI does).
_CHILD_ENV_SCRUB = frozenset({
    # Platform tokens
    "ROBYX_BOT_TOKEN",
    "KAELOPS_BOT_TOKEN",  # legacy alias
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    # AI provider keys (pip doesn't need; author-controlled migrations
    # shouldn't rely on them either — if one ever must, it can re-read
    # from a dedicated config)
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
})

_SENSITIVE_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)",
    re.IGNORECASE,
)
_URL_CREDENTIAL_ENV = frozenset({
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
})
_MIGRATION_ENV_NAMES = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "COMSPEC",
    "VIRTUAL_ENV",
    "PYTHONPATH",
})


def _scrubbed_child_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with platform tokens / AI provider
    keys removed. Used as the ``env=`` argument when spawning pip or
    migration steps during ``apply_update``."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _CHILD_ENV_SCRUB
        and not (
            _SENSITIVE_ENV_NAME.search(key)
            and key not in _URL_CREDENTIAL_ENV
        )
    }


def _migration_child_env() -> dict[str, str]:
    """Return the minimum operational environment for release migrations.

    Unlike pip, migrations do not need index/proxy configuration.  Keeping an
    allowlist prevents an author-controlled migration command from reading
    unrelated bot credentials or newly-added secret variables.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _MIGRATION_ENV_NAMES or key.startswith("LC_")
    }


def _redact_sensitive_text(value: object) -> str:
    """Redact inherited credentials and common inline secret forms."""
    text = str(value)
    for key, secret in os.environ.items():
        if not secret:
            continue
        if _SENSITIVE_ENV_NAME.search(key) or key in _URL_CREDENTIAL_ENV:
            text = text.replace(secret, "[REDACTED]")
        if key in _URL_CREDENTIAL_ENV:
            userinfo = re.match(
                r"(?i)[a-z][a-z0-9+.-]*://([^/@\s]+)@",
                secret,
            )
            if userinfo:
                for credential_part in userinfo.group(1).split(":"):
                    if credential_part:
                        text = text.replace(credential_part, "[REDACTED]")
    # Credentials embedded in a URL may not be present in the current env.
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@",
        r"\1[REDACTED]@",
        text,
    )
    # Also cover diagnostics such as ``token=...`` and ``password: ...``.
    text = re.sub(
        r"(?i)\b(token|secret|password|passwd|api[_-]?key|credential)"
        r"\s*[:=]\s*[^\s,;]+",
        lambda match: "%s=[REDACTED]" % match.group(1),
        text,
    )
    return text


def _safe_diagnostic_tail(value: object, *, max_chars: int = 1600) -> str:
    """Return a bounded, redacted diagnostic tail safe for chat/history."""
    return _redact_sensitive_text(value)[-max_chars:]


def _is_real_async_process(proc: asyncio.subprocess.Process) -> bool:
    """Distinguish real children from lightweight subprocess test doubles."""
    return isinstance(getattr(proc, "pid", None), int) and proc.pid > 1


def _track_update_child(proc: asyncio.subprocess.Process, owner: str) -> bool:
    """Register an isolated updater child immediately after its spawn."""
    if not _is_real_async_process(proc):
        return False
    get_runtime_supervisor().track_process(
        proc,
        owner="update:%s" % owner,
        process_group=sys.platform != "win32",
    )
    return True


async def _await_cleanup_uninterruptibly(task: asyncio.Task):
    """Finish mandatory child/rollback cleanup despite caller cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _terminate_update_child(
    proc: asyncio.subprocess.Process,
    *,
    tracked: bool,
) -> None:
    """Terminate a child tree, await leader reap, and retain orphan evidence."""
    if tracked:
        cleanup = asyncio.create_task(
            get_runtime_supervisor().terminate_process(proc, grace_seconds=2.0),
        )
        await _await_cleanup_uninterruptibly(cleanup)
        return

    # Compatibility path for subprocess test doubles and platforms where a
    # process could not be registered. Real updater children use the branch
    # above and therefore receive process-group termination.
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    wait = getattr(proc, "wait", None)
    if callable(wait):
        result = wait()
        if asyncio.iscoroutine(result):
            await result


async def _communicate_update_child(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    owner: str,
    already_tracked: bool = False,
) -> tuple[bytes, bytes]:
    """Communicate with an updater child and never leak it on timeout/cancel."""
    tracked = already_tracked
    if not tracked:
        try:
            tracked = _track_update_child(proc, owner)
        except BaseException:
            # ``track_process`` can reject during concurrent shutdown after it
            # has registered/signalled the child. Finish that termination
            # before propagating the shutdown/cancellation signal.
            await _terminate_update_child(
                proc,
                tracked=_is_real_async_process(proc),
            )
            raise
    try:
        result = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        await _terminate_update_child(proc, tracked=tracked)
        raise
    if tracked:
        # A clean leader exit is not enough if a descendant kept the captured
        # group alive. ``untrack_process`` deliberately retains that evidence;
        # terminate the captured PGID and fail the phase even if cleanup works,
        # because a child escaped the command's declared lifetime.
        supervisor = get_runtime_supervisor()
        if not supervisor.untrack_process(proc):
            cleanup = asyncio.create_task(
                supervisor.terminate_process(proc, grace_seconds=2.0),
            )
            stopped = await _await_cleanup_uninterruptibly(cleanup)
            if not stopped:
                raise RuntimeError(
                    "%s child left a live descendant process group that "
                    "could not be terminated" % owner
                )
            raise RuntimeError(
                "%s child left a lingering descendant process group; "
                "the group was terminated and the update phase was rejected"
                % owner
            )
    return result


async def _spawn_update_child(
    *argv: str,
    owner: str,
    **kwargs,
) -> tuple[asyncio.subprocess.Process, bool]:
    """Spawn, isolate, and track a child without a cancellation leak window."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        proc = await asyncio.shield(spawn)
    except BaseException as spawn_error:
        # asyncio cancellation can arrive after the OS child exists but before
        # create_subprocess_exec returns its handle. Shield the spawn, recover
        # that handle, then terminate/reap it before propagating cancellation.
        try:
            proc = await _await_cleanup_uninterruptibly(spawn)
        except BaseException:
            raise spawn_error
        tracked = False
        try:
            tracked = _track_update_child(proc, owner)
        finally:
            await _terminate_update_child(proc, tracked=tracked)
        raise spawn_error

    try:
        tracked = _track_update_child(proc, owner)
    except BaseException:
        await _terminate_update_child(
            proc,
            tracked=_is_real_async_process(proc),
        )
        raise
    return proc, tracked


# ── Pre-update data backup + post-update smoke test ──

BACKUPS_DIR_NAME = "backups"
SNAPSHOT_RETENTION = 3
SNAPSHOT_PREFIX = "pre-update-"
UPDATE_TRANSACTION_MARKER = "active-update.json"

# Defense-in-depth cap for the restore path (Pass 2 T067 / P2-72). A
# hostile or accidentally-massive snapshot would otherwise fill the
# disk during extraction. 5 GiB covers realistic DATA_DIR sizes
# (SQLite memory.db + media cache) with generous headroom while still
# refusing a zip-bomb-style archive.
MAX_RESTORE_TOTAL_BYTES = 5 * 1024**3


def _data_snapshot_required() -> bool:
    """Return whether an update must have a verified data snapshot.

    An empty/missing ``data/`` directory has no pre-update runtime state to
    protect. Everything except the snapshot directory itself is runtime state
    and therefore makes the snapshot mandatory.
    """
    try:
        for child in DATA_DIR.iterdir():
            if child.name == BACKUPS_DIR_NAME:
                continue
            if child.is_file() or child.is_symlink():
                return True
            if child.is_dir() and any(descendant.is_file() or descendant.is_symlink()
                                      for descendant in child.rglob("*")):
                return True
        return False
    except FileNotFoundError:
        return False
    except OSError:
        # If we cannot inspect runtime state, fail closed by requiring a
        # snapshot; the snapshot helper will provide the actionable failure.
        return True


def _checkpoint_sqlite_databases() -> tuple[bool, str]:
    """Checkpoint every runtime SQLite WAL before the filesystem snapshot.

    The maintenance gate guarantees there are no application writers while
    this runs.  A busy/corrupt/unreadable database aborts the update rather
    than producing a tarball whose main DB and WAL represent different points
    in time.
    """
    try:
        databases = sorted(
            path for path in DATA_DIR.rglob("*.db")
            if BACKUPS_DIR_NAME not in path.relative_to(DATA_DIR).parts
        )
    except (OSError, ValueError) as exc:
        return False, "could not enumerate SQLite databases: %s" % exc

    for database in databases:
        try:
            connection = sqlite3.connect(str(database), timeout=5.0)
            try:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None and int(row[0]) != 0:
                    return False, "SQLite database remained busy: %s" % database
            finally:
                connection.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return False, "SQLite checkpoint failed for %s: %s" % (database, exc)
    return True, ""


def _write_dependency_marker(marker: Path, fingerprint: str) -> None:
    """Atomically publish a fingerprint only after pip completed successfully."""
    fd, temp_name = tempfile.mkstemp(
        prefix=".%s." % marker.name,
        suffix=".tmp",
        dir=str(marker.parent),
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(fingerprint)
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


def _update_transaction_marker_path() -> Path:
    return DATA_DIR / BACKUPS_DIR_NAME / UPDATE_TRANSACTION_MARKER


def _write_update_transaction_marker(payload: dict) -> None:
    """Durably record a code/data transaction before its first mutation."""
    marker = _update_transaction_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        prefix=".%s." % marker.name,
        suffix=".tmp",
        dir=str(marker.parent),
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        try:
            directory_fd = os.open(marker.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clear_update_transaction_marker() -> None:
    marker = _update_transaction_marker_path()
    marker.unlink(missing_ok=True)
    try:
        directory_fd = os.open(marker.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _snapshot_data_dir(from_version: str, to_version: str) -> Path | None:
    """Tar+gzip ``DATA_DIR`` to a versioned snapshot under ``DATA_DIR/backups/``.

    A failed update can then roll back not only the code (via the
    previous git tag) but also any data mutated by a migration that ran
    before the failure was detected. The snapshot excludes the
    ``backups/`` subdirectory itself to avoid runaway recursive growth
    across successive updates.

    Returns the snapshot path, or ``None`` on failure.  The caller decides
    whether a snapshot is mandatory for the specific update.
    """
    backups = DATA_DIR / BACKUPS_DIR_NAME
    try:
        backups.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Cannot create backups dir %s: %s", backups, e)
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = "%s%s-to-%s-%s.tar.gz" % (SNAPSHOT_PREFIX, from_version, to_version, ts)
    out = backups / name
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".%s" % SNAPSHOT_PREFIX,
            suffix=".tmp",
            dir=str(backups),
        )
        os.close(fd)
        temporary = Path(temporary_name)
    except OSError as e:
        log.warning("Cannot create temporary snapshot in %s: %s", backups, e)
        return None

    def _filter(tarinfo):
        rel = tarinfo.name.lstrip("./")
        if rel == BACKUPS_DIR_NAME or rel.startswith(BACKUPS_DIR_NAME + "/"):
            return None
        return tarinfo

    try:
        with tarfile.open(str(temporary), "w:gz") as tf:
            tf.add(str(DATA_DIR), arcname=".", filter=_filter)
        os.replace(temporary, out)
    except (OSError, tarfile.TarError) as e:
        log.warning("Snapshot of %s failed: %s", DATA_DIR, e)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    log.info("Created data/ snapshot: %s", out)
    return out


def _verify_snapshot(snapshot: Path) -> tuple[bool, str]:
    """Fully read and validate a newly-created snapshot.

    Opening a gzip tar is not sufficient: truncation and CRC errors may only
    surface while member payloads are consumed.  This verification reads every
    regular member and applies the same path/link/size policy as restore.
    """
    if not snapshot.exists() or not snapshot.is_file():
        return False, "snapshot file is missing"
    try:
        archived_files: set[str] = set()
        with tarfile.open(str(snapshot), "r:gz") as tf:
            total_uncompressed = 0
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    return False, "unsafe link member %s" % member.name
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    return False, "unsafe member path %s" % member.name
                rel = member.name.lstrip("./")
                if rel == BACKUPS_DIR_NAME or rel.startswith(BACKUPS_DIR_NAME + "/"):
                    return False, "snapshot recursively contains backups/"
                total_uncompressed += max(member.size, 0)
                if total_uncompressed > MAX_RESTORE_TOTAL_BYTES:
                    return False, "uncompressed size exceeds %d bytes" % MAX_RESTORE_TOTAL_BYTES
                if member.isfile():
                    archived_files.add(
                        member.name[2:] if member.name.startswith("./") else member.name
                    )
                    payload = tf.extractfile(member)
                    if payload is None:
                        return False, "could not read member %s" % member.name
                    while payload.read(1024 * 1024):
                        pass
        expected_files = {
            path.relative_to(DATA_DIR).as_posix()
            for path in DATA_DIR.rglob("*")
            if BACKUPS_DIR_NAME not in path.relative_to(DATA_DIR).parts
            and path.is_file()
        }
        missing = sorted(expected_files - archived_files)
        if missing:
            return False, "snapshot is missing runtime file(s): %s" % ", ".join(missing[:10])
    except (OSError, tarfile.TarError, EOFError) as exc:
        return False, str(exc)
    return True, ""


def _prune_old_snapshots(
    backups: Path,
    keep: int = SNAPSHOT_RETENTION,
    *,
    preserve: Path | None = None,
) -> None:
    """Keep the newest snapshots without deleting the active transaction's."""
    try:
        snaps = sorted(
            backups.glob(SNAPSHOT_PREFIX + "*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    if len(snaps) <= keep:
        return
    remove_count = len(snaps) - keep
    candidates = [p for p in snaps if preserve is None or p != preserve]
    for p in candidates[:remove_count]:
        try:
            p.unlink()
            log.debug("Pruned old snapshot: %s", p)
        except OSError as e:
            log.debug("Could not prune %s: %s", p, e)


def _restore_data_dir(snapshot: Path) -> bool:
    """Restore *snapshot* into ``DATA_DIR`` as the authoritative prior state.

    The snapshot was created with ``backups/`` excluded, so existing
    snapshots in ``DATA_DIR/backups/`` are untouched by the restore. The
    archive is fully validated and extracted to a sibling staging directory
    before current runtime entries are replaced. Files created by a failed
    migration are therefore removed instead of leaking across rollback.
    Returns ``True`` on success.
    """
    if not snapshot.exists():
        log.error("Cannot restore: snapshot %s missing", snapshot)
        return False
    staging: Path | None = None
    previous: Path | None = None
    displaced_new: Path | None = None
    try:
        DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(
            prefix=".robyx-data-restore-", dir=str(DATA_DIR.parent),
        ))
        with tarfile.open(str(snapshot), "r:gz") as tf:
            # Validate members before extraction to avoid half-restoring a
            # corrupt archive on top of DATA_DIR. Reject absolute paths,
            # path-traversal attempts, and cumulative uncompressed size
            # above MAX_RESTORE_TOTAL_BYTES (Pass 2 P2-72 zip-bomb guard).
            data_dir_resolved = DATA_DIR.resolve()
            total_uncompressed = 0
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    log.error(
                        "Refusing to restore snapshot %s: symlink/hardlink member %s",
                        snapshot, member.name,
                    )
                    return False
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    log.error(
                        "Refusing to restore snapshot %s: unsafe member %s",
                        snapshot, member.name,
                    )
                    return False
                target = (DATA_DIR / member.name).resolve()
                try:
                    target.relative_to(data_dir_resolved)
                except ValueError:
                    log.error(
                        "Refusing to restore snapshot %s: member %s escapes data dir",
                        snapshot, member.name,
                    )
                    return False
                total_uncompressed += max(member.size, 0)
                if total_uncompressed > MAX_RESTORE_TOTAL_BYTES:
                    log.error(
                        "Refusing to restore snapshot %s: uncompressed size exceeds %d bytes",
                        snapshot, MAX_RESTORE_TOTAL_BYTES,
                    )
                    return False
            if sys.version_info >= (3, 12):
                tf.extractall(str(staging), filter="data")
            else:  # pragma: no cover - supported legacy Python path
                tf.extractall(str(staging))

        # Commit with same-filesystem directory renames. The current data tree
        # remains intact under ``previous`` until both the staged tree and its
        # retained backups directory are in place. Any failure before commit
        # swaps the original tree back instead of deleting the only good copy.
        previous = Path(tempfile.mkdtemp(
            prefix=".robyx-data-before-restore-", dir=str(DATA_DIR.parent),
        ))
        previous.rmdir()  # reserve a unique absent rename target
        os.replace(DATA_DIR, previous)
        try:
            os.replace(staging, DATA_DIR)
            staging = None

            old_backups = previous / BACKUPS_DIR_NAME
            if old_backups.exists():
                new_backups = DATA_DIR / BACKUPS_DIR_NAME
                if new_backups.exists():
                    raise OSError("restored tree unexpectedly contains backups/")
                os.replace(old_backups, new_backups)
        except Exception:
            # Move the staged/new tree aside, then restore the original path.
            # Keep a unique quarantine if either compensating rename fails so
            # an operator still has both trees available for manual recovery.
            if DATA_DIR.exists():
                displaced_new = Path(tempfile.mkdtemp(
                    prefix=".robyx-data-failed-restore-", dir=str(DATA_DIR.parent),
                ))
                displaced_new.rmdir()
                os.replace(DATA_DIR, displaced_new)
            os.replace(previous, DATA_DIR)
            previous = None
            raise

        # Commit point: DATA_DIR is the complete snapshot and backups have
        # their stable public path again. Cleanup failure is non-fatal; the
        # prior tree is merely left as an explicit recovery quarantine.
        try:
            shutil.rmtree(previous)
            previous = None
        except OSError as cleanup_error:
            log.warning(
                "Restored data but could not remove previous-tree quarantine %s: %s",
                previous, cleanup_error,
            )
    except (OSError, tarfile.TarError, EOFError, ValueError) as e:
        log.error("Restore from %s failed: %s", snapshot, e)
        return False
    finally:
        for temporary in (staging, displaced_new):
            if temporary is not None and temporary.exists():
                try:
                    shutil.rmtree(temporary)
                except OSError:
                    log.warning("Could not clean restore staging path %s", temporary)
    log.info("Restored data/ from %s", snapshot)
    return True


async def _post_update_smoke_test() -> tuple[bool, str]:
    """Run ``<venv>/bin/python bot/bot.py --smoke-test`` to verify the
    new code at least imports cleanly.

    A failed pip install can succeed at the package-resolution level
    while still leaving the venv in a broken state (e.g. a transitive
    dependency conflict that only surfaces at import time). Catching
    that here lets the caller roll back instead of restarting into a
    broken bot.

    We invoke ``bot/bot.py`` exactly the way the production service
    launches it, so ``sys.path[0]`` is ``bot/`` and ``import _bootstrap``
    resolves cleanly. The ``--smoke-test`` flag makes ``bot.py`` exit 0
    after all module-level imports have completed, before ``main()``
    opens network sockets or acquires the pid lock.
    """
    venv_bin = "Scripts" if sys.platform == "win32" else "bin"
    py_name = "python.exe" if sys.platform == "win32" else "python"
    py = PROJECT_ROOT / ".venv" / venv_bin / py_name
    if not py.exists():
        return False, "venv python not found at %s" % py

    bot_py = PROJECT_ROOT / "bot" / "bot.py"
    proc, tracked = await _spawn_update_child(
        str(py), str(bot_py), "--smoke-test",
        owner="smoke-test",
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    smoke_timeout = int(os.environ.get("SMOKE_TEST_TIMEOUT_SECONDS", "60"))
    try:
        stdout, stderr = await _communicate_update_child(
            proc,
            timeout=smoke_timeout,
            owner="smoke-test",
            already_tracked=tracked,
        )
    except asyncio.TimeoutError:
        return False, "smoke test timed out after %ds" % smoke_timeout

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or stdout.decode(errors="replace")).strip()
        return False, "bot.py --smoke-test exited %d: %s" % (
            proc.returncode,
            _safe_diagnostic_tail(err, max_chars=500),
        )
    return True, ""


def _check_python_syntax_in_repo() -> tuple[bool, str]:
    """Compile every ``.py`` file under ``bot/`` to detect SyntaxError
    (typically caused by raw ``<<<<<<<`` / ``=======`` / ``>>>>>>>``
    conflict markers introduced by a ``git stash pop`` that resolved
    only partially). Returns ``(ok, error_message)``.

    Cheap parse-only check — no imports, no subprocess. Used by
    :func:`apply_update` as belt-and-braces after step-6 stash pop:
    the full smoke test at step 5.5 ran BEFORE the pop and cannot
    catch conflict markers introduced AFTER it.
    """
    bot_dir = PROJECT_ROOT / "bot"
    if not bot_dir.is_dir():
        return True, ""
    for py in sorted(bot_dir.rglob("*.py")):
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return False, "could not read %s: %s" % (py, exc)
        try:
            compile(source, str(py), "exec")
        except SyntaxError as exc:
            rel = py.relative_to(PROJECT_ROOT)
            return False, "SyntaxError in %s line %d: %s" % (
                rel, exc.lineno or 0, exc.msg,
            )
    return True, ""


# ── Release note parser ──


def _parse_release_notes(text: str) -> dict:
    """Parse a release note file with YAML-like frontmatter."""
    result = {
        "version": "",
        "min_compatible": "0.0.0",
        "breaking": False,
        "requires_migration": False,
        "body": "",
        "migration_steps": [],
    }

    # Parse frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if key == "version":
                result["version"] = val
            elif key == "min_compatible":
                result["min_compatible"] = val
            elif key == "breaking":
                result["breaking"] = val.lower() == "true"
            elif key == "requires_migration":
                result["requires_migration"] = val.lower() == "true"
        result["body"] = text[fm_match.end():]
    else:
        result["body"] = text

    # Extract migration steps (lines starting with a number after ## Migration)
    migration_match = re.search(
        r"## Migration\s*\n(.*?)(?:\n## |\Z)", result["body"], re.DOTALL
    )
    if migration_match:
        for line in migration_match.group(1).splitlines():
            line = line.strip()
            # Match "1. Run: `command`" or "1. `command`" or "- `command`"
            cmd_match = re.match(r"(?:\d+\.\s*(?:Run:\s*)?|[-*]\s*)`([^`]+)`", line)
            if cmd_match:
                result["migration_steps"].append(cmd_match.group(1))

    return result


# ── Git operations ──


async def _git(*args, check=True) -> subprocess.CompletedProcess:
    """Run a git command in the project root without blocking the event loop."""
    proc, tracked = await _spawn_update_child(
        "git", *args,
        owner="git:%s" % (args[0] if args else "command"),
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout_b, stderr_b = await _communicate_update_child(
            proc,
            timeout=60,
            owner="git:%s" % (args[0] if args else "command"),
            already_tracked=tracked,
        )
    except asyncio.TimeoutError:
        raise subprocess.TimeoutExpired(["git", *args], 60)
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", *args], stdout, stderr,
        )
    return subprocess.CompletedProcess(
        ["git", *args], proc.returncode, stdout, stderr,
    )


class StashPopConflict(Exception):
    """Raised by :func:`_safe_stash_pop` (when ``strict=True``) if a
    ``git stash pop`` left unmerged paths in the working tree.

    The success path of :func:`apply_update` catches this and triggers
    a code+state rollback so the bot is not restarted into a file with
    raw ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` conflict markers — the
    failure mode behind the v0.28.0 Linux non-restart incident.

    ``unmerged_paths`` carries the list of conflicted files so callers
    can surface them in the rollback message.
    """

    def __init__(self, returncode: int, unmerged_paths: list[str], stderr: str):
        self.returncode = returncode
        self.unmerged_paths = unmerged_paths
        self.stderr = stderr
        super().__init__(
            "git stash pop left unmerged paths (rc=%d): %s — stash is "
            "preserved; conflict markers are in the working tree."
            % (returncode, ", ".join(unmerged_paths) or "(none)"),
        )


async def _safe_stash_pop(*, strict: bool = False) -> None:
    """Pop the most recent stash and log a WARNING if it fails.

    All apply-update error paths call this to restore the user's local
    changes after rolling back. A silent failure (the prior pattern) left
    the changes stranded in the stash list with no signal — making
    "where did my edits go?" debugging painful. The pop is still
    best-effort: we don't raise, because we're already on an error path
    and the broader update outcome is what matters. But the operator
    needs to know to run ``git stash list`` / ``git stash pop`` manually.

    Conflict detection: a non-zero exit most commonly means the stash
    would overwrite changes in the now-current code (i.e. the user's
    local edits conflict with the new release). Git leaves conflict
    markers and **keeps the stash** in that case, but the index is now
    in an unmerged state. If we don't surface this loudly, the *next*
    auto-update will hit the pre-flight gate in ``apply_update`` and
    refuse to run until the operator resolves the conflict. Log with
    explicit file list and recovery instructions so "why did auto-update
    stop working?" is answered before the next attempt.

    Spec 007.2 / v0.28.2 — ``strict=True`` raises :class:`StashPopConflict`
    on the unmerged-paths case so callers on the SUCCESS path of an
    update can react with a rollback instead of restarting the bot into
    a file with raw conflict markers (the v0.28.0 Linux crashloop).
    Error-path callers leave ``strict=False`` to preserve best-effort
    semantics.
    """
    result = await _git("stash", "pop", check=False)
    if result.returncode == 0:
        return

    stderr_out = _safe_diagnostic_tail(
        (result.stderr or result.stdout).strip() or "(no output)",
    )
    unmerged = await _git("ls-files", "--unmerged", check=False)
    unmerged_paths = sorted({
        line.split("\t", 1)[1] for line in unmerged.stdout.splitlines()
        if "\t" in line
    })

    if unmerged_paths:
        log.error(
            "git stash pop LEFT UNMERGED PATHS after rc=%d: %s\n"
            "The local changes still exist in the stash AND conflict markers "
            "are in the working tree. Resolve manually: inspect with "
            "`git status`, edit the files to remove <<<<<<</=======/>>>>>>> "
            "markers, `git add <resolved-file>`, then `git stash drop` when "
            "happy. Until resolved, subsequent `apply_update` calls will "
            "refuse to run (pre-flight gate). stderr: %s",
            result.returncode,
            ", ".join(unmerged_paths),
            stderr_out,
        )
        if strict:
            raise StashPopConflict(
                result.returncode, unmerged_paths, stderr_out,
            )
    else:
        log.warning(
            "git stash pop failed (rc=%d): %s — local changes may be stranded "
            "in the stash list; run `git stash list` to inspect, "
            "`git stash pop` to recover",
            result.returncode,
            stderr_out,
        )


async def _preflight_git_state() -> tuple[bool, str]:
    """Refuse to start an update when the repo is in a pre-existing
    broken-merge state.

    Today's pattern (observed in the field 2026-04-21): an earlier
    auto-update's ``git stash pop`` conflicted silently, leaving the
    index with unmerged stages and no ``MERGE_HEAD``. Every subsequent
    update would then fail on ``git pull`` with "Pulling is not
    possible because you have unmerged files", roll back, and leave
    the user stuck in a loop until a human intervened.

    Pre-flight blocks: unmerged index entries, or the marker files/dirs
    left behind by in-progress merge / cherry-pick / rebase operations.
    Returns ``(ok, message)``. ``ok=True`` means the update can proceed.
    """
    unmerged = await _git("ls-files", "--unmerged", check=False)
    unmerged_paths = sorted({
        line.split("\t", 1)[1] for line in unmerged.stdout.splitlines()
        if "\t" in line
    })
    if unmerged_paths:
        return False, (
            "repository has %d unmerged path(s) from a prior interrupted "
            "operation: %s. Resolve them (edit the files, `git add` each, "
            "commit or `git reset --mixed HEAD` if the changes are stale) "
            "before re-running the update."
            % (len(unmerged_paths), ", ".join(unmerged_paths))
        )

    # In-progress merge / cherry-pick / rebase leave these behind.
    in_progress_markers = {
        "merge": PROJECT_ROOT / ".git" / "MERGE_HEAD",
        "cherry-pick": PROJECT_ROOT / ".git" / "CHERRY_PICK_HEAD",
        "revert": PROJECT_ROOT / ".git" / "REVERT_HEAD",
        "rebase (apply)": PROJECT_ROOT / ".git" / "rebase-apply",
        "rebase (merge)": PROJECT_ROOT / ".git" / "rebase-merge",
    }
    for kind, marker in in_progress_markers.items():
        if marker.exists():
            return False, (
                "a git %s operation is in progress (%s exists). "
                "Finish it (`git %s --continue`) or abort it "
                "(`git %s --abort`) before re-running the update."
                % (kind, marker, kind.split()[0], kind.split()[0])
            )

    return True, ""


async def _rollback_code_to(commit_sha: str) -> tuple[bool, str]:
    """Reset ``main`` to the exact pre-update commit and verify ``HEAD``.

    Rollback is deliberately commit-anchored rather than version/tag-anchored:
    a tag can be missing, moved, or point somewhere other than the commit that
    was actually running before the update.  Every command is inspected and a
    failed verification is returned to the caller so it can *avoid* restoring
    data written for a different code/schema version.
    """
    checkout = await _git("checkout", "main", check=False)
    if checkout.returncode != 0:
        return False, "could not attach HEAD to main: %s" % (
            checkout.stderr.strip() or checkout.stdout.strip() or "unknown git error"
        )

    reset = await _git("reset", "--hard", commit_sha, check=False)
    if reset.returncode != 0:
        return False, "git reset --hard %s failed: %s" % (
            commit_sha,
            reset.stderr.strip() or reset.stdout.strip() or "unknown git error",
        )

    head = await _git("rev-parse", "HEAD", check=False)
    actual = head.stdout.strip() if head.returncode == 0 else ""
    if head.returncode != 0 or actual != commit_sha:
        return False, "rollback HEAD verification failed: expected %s, got %s" % (
            commit_sha, actual or "(unavailable)",
        )
    return True, ""


@dataclass(frozen=True)
class UpdateTarget:
    """A target tag resolved and verified against the configured ``origin``."""

    version: str
    tag: str
    object_sha: str
    commit_sha: str
    notes: dict | None


def _validate_update_version(version: str) -> str:
    """Validate a version before using it in git refs and release paths."""
    candidate = version.strip()
    if not candidate or candidate.startswith("v"):
        raise ValueError("version must not be empty or include the 'v' tag prefix")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", candidate):
        raise ValueError("version contains characters not allowed in a release tag")
    try:
        Version(candidate)
    except Exception as exc:
        raise ValueError("invalid release version %r: %s" % (candidate, exc)) from exc
    return candidate


async def _resolve_exact_target(version: str) -> UpdateTarget:
    """Fetch and resolve exactly ``origin``'s ``v<version>`` tag.

    The tag is fetched into an updater-owned ref instead of the local tag
    namespace.  This avoids installing an old/stale local tag and lets us
    compare the fetched object with the object SHA advertised by ``origin``.
    Annotated tags are then peeled to the exact commit that will be installed.
    Before touching the working tree we also prove that that commit's VERSION
    file and release-note metadata agree with the requested version.
    """
    version = _validate_update_version(version)
    tag = "v" + version
    remote_ref = "refs/tags/" + tag
    candidate_ref = "refs/robyx/updates/" + tag

    advertised = await _git(
        "ls-remote", "--tags", "--refs", "origin", remote_ref,
        check=False,
    )
    if advertised.returncode != 0:
        raise RuntimeError("could not query origin for %s: %s" % (
            tag,
            advertised.stderr.strip() or advertised.stdout.strip() or "unknown git error",
        ))
    matches = []
    for line in advertised.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == remote_ref:
            matches.append(parts[0].lower())
    if len(matches) != 1 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", matches[0]):
        raise RuntimeError("origin does not advertise exactly one valid %s tag" % tag)
    advertised_object = matches[0]

    fetched = await _git(
        "fetch", "--no-tags", "origin",
        "+%s:%s" % (remote_ref, candidate_ref),
        check=False,
    )
    if fetched.returncode != 0:
        raise RuntimeError("fetch of exact tag %s failed: %s" % (
            tag,
            fetched.stderr.strip() or fetched.stdout.strip() or "unknown git error",
        ))

    fetched_object = await _git("rev-parse", candidate_ref, check=False)
    actual_object = fetched_object.stdout.strip().lower()
    if fetched_object.returncode != 0 or actual_object != advertised_object:
        raise RuntimeError(
            "fetched tag object verification failed for %s: expected %s, got %s"
            % (tag, advertised_object, actual_object or "(unavailable)")
        )

    peeled = await _git("rev-parse", candidate_ref + "^{commit}", check=False)
    commit_sha = peeled.stdout.strip().lower()
    if peeled.returncode != 0 or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha,
    ):
        raise RuntimeError("could not peel %s to a commit" % tag)

    tagged_version = await _git("show", "%s:VERSION" % commit_sha, check=False)
    if tagged_version.returncode != 0:
        raise RuntimeError("%s commit has no readable VERSION file" % tag)
    version_in_commit = tagged_version.stdout.strip()
    if version_in_commit != version:
        raise RuntimeError(
            "%s VERSION mismatch: requested %s, commit declares %s"
            % (tag, version, version_in_commit or "(empty)")
        )

    notes = None
    release_path = "releases/%s.md" % version
    tagged_notes = await _git("show", "%s:%s" % (commit_sha, release_path), check=False)
    if tagged_notes.returncode == 0:
        notes = _parse_release_notes(tagged_notes.stdout)
        if notes.get("version") and notes["version"] != version:
            raise RuntimeError(
                "%s metadata mismatch: requested %s, release notes declare %s"
                % (release_path, version, notes["version"])
            )

    return UpdateTarget(version, tag, advertised_object, commit_sha, notes)


async def _verify_installed_target(target: UpdateTarget) -> tuple[bool, str]:
    """Verify that the worktree is still at *target* and declares its version."""
    head = await _git("rev-parse", "HEAD", check=False)
    actual_head = head.stdout.strip().lower() if head.returncode == 0 else ""
    if actual_head != target.commit_sha.lower():
        return False, "HEAD mismatch: expected %s, got %s" % (
            target.commit_sha, actual_head or "(unavailable)",
        )
    try:
        installed_version = get_current_version()
    except OSError as exc:
        return False, "could not read installed VERSION: %s" % exc
    if installed_version != target.version:
        return False, "installed VERSION mismatch: expected %s, got %s" % (
            target.version, installed_version or "(empty)",
        )
    return True, ""


async def _checkout_exact_target(target: UpdateTarget) -> tuple[bool, str]:
    """Move attached ``main`` to the resolved target commit and verify it."""
    reset = await _git("reset", "--hard", target.commit_sha, check=False)
    if reset.returncode != 0:
        return False, "git reset to %s failed: %s" % (
            target.tag,
            reset.stderr.strip() or reset.stdout.strip() or "unknown git error",
        )
    return await _verify_installed_target(target)


async def fetch_remote_tags() -> list[str]:
    """List version tags on origin, sorted ascending.

    Uses ``git ls-remote`` so we only do a lightweight ref lookup instead of
    a full ``git fetch --tags`` (which transfers tag objects and grows slower
    as the number of releases grows).
    """
    result = await _git("ls-remote", "--tags", "--refs", "origin", "v*")
    seen = set()
    for line in result.stdout.splitlines():
        # Format: "<sha>\trefs/tags/<name>"; --refs strips peeled "^{}" lines.
        parts = line.split("refs/tags/", 1)
        if len(parts) != 2:
            continue
        name = parts[1].strip()
        if name:
            seen.add(name)

    def _key(tag):
        try:
            return Version(tag.lstrip("v"))
        except Exception:
            return Version("0")

    return sorted(seen, key=_key)


def _get_latest_remote_version(tags: list[str]) -> str | None:
    """Return the highest semver tag, or None."""
    if not tags:
        return None
    # Tags are sorted ascending by git, last is highest
    latest_tag = tags[-1]
    return latest_tag.lstrip("v")


async def _get_release_notes_for(version: str, tags: list[str]) -> dict | None:
    """Get release notes from the tagged commit's releases/<version>.md."""
    tag = "v" + version
    if tag not in tags:
        return None

    # fetch_remote_tags() uses ls-remote and does not download tag objects,
    # so the tag may not exist locally yet. Fetch just this one tag.
    show = await _git("show", "%s:releases/%s.md" % (tag, version), check=False)
    if show.returncode != 0:
        try:
            await _git("fetch", "origin", "tag", tag, "--no-tags")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        show = await _git("show", "%s:releases/%s.md" % (tag, version), check=False)
        if show.returncode != 0:
            return None

    return _parse_release_notes(show.stdout)


# ── Check for updates ──


async def check_for_updates() -> dict | None:
    """Check and persist notification state under a shared runtime lease."""
    async with get_maintenance_gate().shared():
        return await _check_for_updates()


async def _check_for_updates() -> dict | None:
    """Check if a new version is available.

    Returns a dict with update info, or None if up to date.
    Result keys: version, current, release_notes, status
    Status is one of: available, breaking, incompatible
    """
    state = _load_state()
    current = get_current_version()

    try:
        tags = await fetch_remote_tags()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Failed to fetch tags: %s", e)
        return None

    latest = _get_latest_remote_version(tags)
    if not latest:
        return None

    if Version(latest) <= Version(current):
        return None

    # Already notified for this version
    if latest in state.get("notified_versions", []):
        return None

    # Get release notes
    notes = await _get_release_notes_for(latest, tags)

    # Determine status
    status = "available"
    if notes:
        if notes["breaking"]:
            status = "breaking"
        elif Version(current) < Version(notes["min_compatible"]):
            status = "incompatible"

    now = datetime.now(timezone.utc).isoformat()
    state["last_check"] = now
    state["notified_versions"].append(latest)
    _save_state(state)

    return {
        "version": latest,
        "current": current,
        "release_notes": notes,
        "status": status,
    }


async def get_pending_update() -> dict | None:
    """Resolve a pending release under a shared runtime lease."""
    async with get_maintenance_gate().shared():
        return await _get_pending_update()


async def _get_pending_update() -> dict | None:
    """Check if there is an update that can be applied (already notified, not yet applied).

    Unlike :func:`check_for_updates`, this doesn't re-notify — it just
    verifies the latest remote version is newer than current, and that
    the release notes allow auto-application (non-breaking, compatible).
    """
    current = get_current_version()

    try:
        tags = await fetch_remote_tags()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Failed to fetch tags: %s", e)
        return None

    latest = _get_latest_remote_version(tags)
    if not latest or Version(latest) <= Version(current):
        return None

    notes = await _get_release_notes_for(latest, tags)
    if notes and notes["breaking"]:
        return None  # Breaking updates cannot be auto-applied
    if notes and Version(current) < Version(notes["min_compatible"]):
        return None  # Incompatible

    return {
        "version": latest,
        "current": current,
        "release_notes": notes,
    }


# ── v0.16 personal-data migration (pre-update) ──


def migrate_personal_data_to_data_dir() -> list[str]:
    """v0.16 pre-update migration: copy tracked runtime files to ``data/``.

    Before v0.16, Robyx shipped personal runtime files committed at the
    repo root (``tasks.md``, ``specialists.md``, ``agents/<name>.md``,
    ``specialists/<name>.md``). v0.16 moves these under ``data/`` which is
    gitignored. On the user's live runtime install, the updater must copy
    these files into ``data/`` **before** the target checkout/reset removes
    them from the working tree — otherwise the update drops them and the
    fleet boots with an empty state.

    Idempotency guarantee: files that already exist under ``data/`` are
    never overwritten. Safe to run repeatedly, safe on fresh clones (no-op).

    Returns the list of repo-root-relative paths that were actually copied.
    """
    moved: list[str] = []
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for name in ("tasks.md", "specialists.md"):
        src = PROJECT_ROOT / name
        dst = data_dir / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError as e:
                log.warning("Could not migrate %s to data/: %s", name, e)

    for subdir in ("agents", "specialists"):
        src_dir = PROJECT_ROOT / subdir
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        dst_dir = data_dir / subdir
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(src_dir.glob("*.md")):
            dst = dst_dir / src.name
            if dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
                moved.append("%s/%s" % (subdir, src.name))
            except OSError as e:
                log.warning(
                    "Could not migrate %s/%s to data/: %s", subdir, src.name, e
                )

    return moved


# ── Apply update ──


async def apply_update(
    version: str,
    notify_fn=None,
    manager=None,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Acquire the one process-wide maintenance transaction and update.

    Normal updates fail closed while runtime work is active. ``force=True``
    first blocks new shared leases, drains all supervised process groups, and
    waits for their delivery/state writers before the snapshot is allowed.
    """
    try:
        version = _validate_update_version(version)
    except ValueError as exc:
        return False, str(exc)

    supervisor = get_runtime_supervisor()

    async def _quiesce_runtime() -> None:
        if supervisor.process_count == 0:
            return
        if not force:
            raise MaintenanceBusyError(
                "runtime agents or scheduled tasks are still active",
            )
        drained = await supervisor.drain_processes(grace_seconds=5.0)
        if not drained:
            raise MaintenanceBusyError(
                "one or more runtime process groups could not be drained",
            )
        # Give supervised delivery watchers an event-loop turn to reconcile
        # locks/state and release their shared maintenance leases.
        await asyncio.sleep(0)

    gate = get_maintenance_gate()
    update_succeeded = False

    async def _finalize_transaction_marker() -> None:
        if update_succeeded and _update_transaction_marker_path().exists():
            _clear_update_transaction_marker()

    try:
        async with gate.exclusive(
            quiesce=_quiesce_runtime,
            finalize=_finalize_transaction_marker,
            wait_timeout=30.0 if force else 0.05,
        ):
            result = await _apply_update_transaction(
                version,
                notify_fn=notify_fn,
                manager=manager,
            )
            update_succeeded = result[0]
        if result[0]:
            return result
        return False, _safe_diagnostic_tail(result[1], max_chars=4000)
    except MaintenanceBusyError as exc:
        return False, "Update blocked: %s" % _safe_diagnostic_tail(exc)
    except OSError as exc:
        return False, (
            "Update installed and verified, but could not finalize the "
            "recovery marker: %s; refusing restart."
            % _safe_diagnostic_tail(exc)
        )
    except Exception as exc:
        return False, "Update failed before commit: %s" % _safe_diagnostic_tail(
            exc,
            max_chars=4000,
        )


async def _apply_update_transaction(
    version: str,
    notify_fn=None,
    manager=None,
) -> tuple[bool, str]:
    """Install and verify the exact ``origin`` tag for *version*.

    The update is a small transaction: preserve local work, capture the exact
    pre-update commit, resolve/fetch the requested remote tag, require a
    verified runtime-data snapshot when data exists, install the peeled target
    commit, migrate/install/smoke-test, restore the stash, verify HEAD+VERSION,
    and only then record success.  Any post-checkout failure rolls code back to
    the captured commit.  Data is restored only after that rollback succeeds.

    Args:
        version: Target version string (e.g. "0.2.0")
        notify_fn: Optional async callback(message) for progress updates
        manager: The live :class:`AgentManager`. Required for the
            diff-driven session invalidation step (v0.15.1+) to actually
            persist — passing the manager lets the updater route the
            reset through ``manager.reset_sessions(...)`` instead of
            mutating ``state.json`` directly (which gets clobbered by
            the running bot's next ``save_state()`` call). When ``None``,
            invalidation is skipped with a warning.

    Returns:
        (success, message) tuple
    """
    try:
        version = _validate_update_version(version)
    except ValueError as exc:
        return False, str(exc)

    current = get_current_version()
    pre_pull_sha: str | None = None
    snapshot: Path | None = None
    has_stash = False
    stash_pending = False
    code_mutated = False
    data_mutated = False
    transaction_marker_active = False
    pre_update_branch = ""
    initial_pre_update_sha = ""
    branch_switched = False

    async def notify(msg):
        msg = _safe_diagnostic_tail(msg, max_chars=4000)
        if notify_fn:
            await notify_fn(msg)
        log.info(msg)

    def record_failure(error: str) -> None:
        """Best-effort audit entry; never obscure the primary failure."""
        error = _safe_diagnostic_tail(error, max_chars=4000)
        try:
            state = _load_state()
            state.setdefault("update_history", []).append({
                "version": version,
                "from_version": current,
                "date": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "error": error,
            })
            _save_state(state)
        except Exception:
            log.exception("Could not record failed update attempt")

    def mark_transaction(phase: str, *, target_commit: str | None = None) -> None:
        nonlocal transaction_marker_active
        if pre_pull_sha is None:
            raise RuntimeError("cannot mark update without a rollback commit")
        _write_update_transaction_marker({
            "schema": 1,
            "phase": phase,
            "target_version": version,
            "target_commit": target_commit,
            "pre_update_commit": pre_pull_sha,
            "initial_commit": initial_pre_update_sha or pre_pull_sha,
            "pre_update_branch": pre_update_branch or None,
            "pre_update_version": current,
            "snapshot": str(snapshot) if snapshot is not None else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        transaction_marker_active = True

    def clear_transaction_marker() -> str:
        nonlocal transaction_marker_active
        if not transaction_marker_active:
            return ""
        try:
            _clear_update_transaction_marker()
        except OSError as exc:
            return " could not clear recovery marker: %s" % exc
        transaction_marker_active = False
        return ""

    async def abort_before_install(reason: str) -> tuple[bool, str]:
        nonlocal branch_switched
        reason = _safe_diagnostic_tail(reason, max_chars=4000)
        if branch_switched:
            if pre_update_branch:
                restore_branch = await _git(
                    "checkout", pre_update_branch, check=False,
                )
            else:
                restore_branch = await _git(
                    "checkout", "--detach", initial_pre_update_sha, check=False,
                )
            if restore_branch.returncode != 0:
                critical = (
                    "%s\nCRITICAL: could not restore the pre-update branch; "
                    "the recovery marker and stash were preserved."
                    % reason
                )
                get_maintenance_gate().poison(critical)
                record_failure(critical)
                return False, critical
            branch_switched = False
        if stash_pending:
            await _safe_stash_pop()
        reason += clear_transaction_marker()
        record_failure(reason)
        return False, reason

    async def rollback_failure(
        reason: str,
        *,
        restore_stash: bool = True,
    ) -> tuple[bool, str]:
        """Rollback code first; restore data only once code is proven safe."""
        nonlocal branch_switched
        reason = _safe_diagnostic_tail(reason, max_chars=4000)
        if pre_pull_sha is None or (not code_mutated and not data_mutated):
            return await abort_before_install(reason)

        if code_mutated:
            rollback_ok, rollback_error = await _rollback_code_to(pre_pull_sha)
        else:
            rollback_ok, rollback_error = True, ""

        if not rollback_ok:
            critical = (
                "%s\nCRITICAL: code rollback to %s failed: %s. "
                "The data snapshot was NOT restored and the local-change "
                "stash was preserved to avoid mixing unknown code, data, and WIP. "
                "Manual recovery is required."
                % (reason, pre_pull_sha, rollback_error)
            )
            log.critical(critical)
            get_maintenance_gate().poison(critical)
            record_failure(critical)
            return False, critical

        restore_error = ""
        restore_ok = True
        if snapshot is not None and not _restore_data_dir(snapshot):
            restore_ok = False
            restore_error = (
                " Code rollback succeeded, but restoring data from %s failed; "
                "the snapshot was preserved for manual recovery."
                % snapshot
            )
            log.critical(restore_error.strip())

        if restore_ok and snapshot is not None and manager is not None:
            reload_method = getattr(
                manager,
                "reload_state_after_maintenance_restore",
                None,
            )
            if not callable(reload_method):
                restore_ok = False
                restore_error += (
                    " Data was restored, but the live agent state has no "
                    "authoritative maintenance reload API. Runtime writes "
                    "were disabled to prevent the restored files from being "
                    "clobbered."
                )
            else:
                try:
                    reload_result = reload_method()
                    if asyncio.iscoroutine(reload_result):
                        await reload_result
                except BaseException as exc:
                    restore_ok = False
                    restore_error += (
                        " Data was restored, but reloading live agent state "
                        "failed: %s. Runtime writes were disabled."
                        % _safe_diagnostic_tail(exc)
                    )
            if not restore_ok:
                get_maintenance_gate().poison(restore_error.strip())
                log.critical(restore_error.strip())

        if branch_switched:
            if pre_update_branch:
                restore_branch = await _git(
                    "checkout", pre_update_branch, check=False,
                )
            else:
                restore_branch = await _git(
                    "checkout", "--detach", initial_pre_update_sha, check=False,
                )
            if restore_branch.returncode != 0:
                restore_ok = False
                restore_error += (
                    " Code/data rollback succeeded, but restoring the original "
                    "branch or detached HEAD failed; the recovery marker and "
                    "stash were preserved for manual recovery."
                )
            else:
                branch_switched = False

        if restore_ok:
            try:
                rollback_version = get_current_version()
            except OSError as exc:
                restore_ok = False
                restore_error += " Could not read VERSION after rollback: %s" % exc
            else:
                if rollback_version != current:
                    restore_ok = False
                    restore_error += (
                        " Rollback VERSION verification failed: expected %s, got %s."
                        % (current, rollback_version or "(empty)")
                    )

        if not restore_ok:
            get_maintenance_gate().poison(restore_error.strip() or reason)
            log.critical(restore_error.strip() or reason)

        if restore_stash and stash_pending and restore_ok:
            await _safe_stash_pop()

        marker_error = ""
        if restore_ok:
            marker_error = clear_transaction_marker()
        result = reason + restore_error + marker_error
        record_failure(result)
        return False, result

    # 0. Pre-flight: refuse to run if the repo is in a pre-existing
    # broken-merge state. Without this gate, a prior failed stash pop
    # that left unmerged index entries (see _safe_stash_pop) would
    # cause every subsequent update to fail on `git pull`, roll back,
    # and leave the user stuck in a loop. Surface the problem clearly
    # up front so the operator can resolve it once.
    ok, preflight_msg = await _preflight_git_state()
    if not ok:
        await notify("Pre-flight check failed: %s" % preflight_msg)
        return False, "Pre-flight check failed: %s" % preflight_msg

    # Capture a read-only recovery anchor and snapshot before the very first
    # mutation (stash / branch switch / personal-data relocation). A hard
    # process death anywhere after the marker commit therefore makes the next
    # bootstrap fail closed instead of starting an unverified half-update.
    head_check = await _git(
        "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
    )
    pre_update_branch = (
        head_check.stdout.strip() if head_check.returncode == 0 else ""
    )
    initial_head = await _git("rev-parse", "HEAD", check=False)
    if initial_head.returncode != 0 or not initial_head.stdout.strip():
        error = initial_head.stderr.strip() or initial_head.stdout.strip()
        return False, (
            "could not capture pre-update HEAD before mutation: %s"
            % (error or "unknown git error")
        )
    pre_pull_sha = initial_head.stdout.strip().lower()
    initial_pre_update_sha = pre_pull_sha
    current = get_current_version()

    if _data_snapshot_required():
        sqlite_ok, sqlite_error = _checkpoint_sqlite_databases()
        if not sqlite_ok:
            return False, "Data snapshot preflight failed: %s" % sqlite_error
        snapshot = _snapshot_data_dir(current, version)
        if snapshot is None:
            return False, "Data snapshot failed; update aborted before changing code"
        snapshot_ok, snapshot_error = _verify_snapshot(snapshot)
        if not snapshot_ok:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass
            snapshot = None
            return False, (
                "Data snapshot verification failed; update aborted: %s"
                % snapshot_error
            )
        _prune_old_snapshots(
            DATA_DIR / BACKUPS_DIR_NAME,
            preserve=snapshot,
        )
        await notify("Created and verified data snapshot: %s" % snapshot.name)

    try:
        mark_transaction("stash")
    except OSError as exc:
        return False, "Could not create durable update recovery marker: %s" % exc

    # 1. Stash local changes. A failed stash is itself a hard stop: moving
    # main after that could overwrite WIP that was never preserved.
    stash_result = await _git("stash", "--include-untracked", check=False)
    if stash_result.returncode != 0:
        error = _safe_diagnostic_tail(
            stash_result.stderr.strip() or stash_result.stdout.strip(),
        )
        reason = "git stash failed and may have partially mutated the workspace: %s" % (
            error or "unknown git error",
        )
        reason += (
            " The recovery marker was preserved and runtime writes were "
            "disabled until the repository is inspected."
        )
        get_maintenance_gate().poison(reason)
        record_failure(reason)
        return False, reason
    has_stash = "No local changes" not in stash_result.stdout
    stash_pending = has_stash

    try:
        # 2. Keep HEAD attached to main for service compatibility, then
        # capture the exact commit/version that rollback must restore.
        current_branch = pre_update_branch
        if current_branch != "main":
            if not current_branch:
                await notify("Detached HEAD detected — reattaching to main")
            else:
                await notify(
                    "On branch '%s', update targets 'main' — switching"
                    % current_branch,
                )
            # A non-zero checkout can still have touched the index/worktree;
            # mark the compensating branch restore before invoking git.
            branch_switched = True
            attach = await _git("checkout", "main", check=False)
            if attach.returncode != 0:
                error = attach.stderr.strip() or attach.stdout.strip()
                return await abort_before_install("could not switch to main: %s" % error)

        pre_pull = await _git("rev-parse", "HEAD", check=False)
        if pre_pull.returncode != 0 or not pre_pull.stdout.strip():
            error = pre_pull.stderr.strip() or pre_pull.stdout.strip()
            return await abort_before_install(
                "could not capture pre-update HEAD; refusing an update without "
                "a rollback anchor: %s" % (error or "unknown git error")
            )
        pre_pull_sha = pre_pull.stdout.strip().lower()
        mark_transaction("workspace-prepare")

        # 3. Resolve the requested remote tag, not current main.  This is
        # read-only with respect to the working tree and therefore happens
        # before snapshot/install.
        await notify("Resolving exact release tag v%s..." % version)
        try:
            target = await _resolve_exact_target(version)
        except Exception as exc:
            return await abort_before_install("Exact tag verification failed: %s" % exc)

        # v0.16 personal-data relocation still runs before code changes.
        try:
            # Set before invoking the synchronous copier: cancellation-like
            # BaseExceptions or I/O faults can arrive after a partial copy.
            data_mutated = True
            moved = migrate_personal_data_to_data_dir()
            if moved:
                await notify(
                    "Migrated %d file(s) to data/: %s"
                    % (len(moved), ", ".join(moved))
                )
        except Exception as exc:
            log.warning(
                "Personal-data migration raised — continuing: %s",
                _safe_diagnostic_tail(exc),
            )

        # 4. Install exactly the peeled tag commit. Set the mutation flag
        # before reset because even a failing reset may partially touch files.
        await notify("Installing %s at commit %s..." % (target.tag, target.commit_sha[:12]))
        mark_transaction("checkout", target_commit=target.commit_sha)
        code_mutated = True
        installed, install_error = await _checkout_exact_target(target)
        if not installed:
            return await rollback_failure("Exact target install failed: %s" % install_error)

        # 5. Run migration steps from release notes read out of the verified
        # target commit, never from an ambiguous main/worktree revision.
        notes = target.notes
        if notes and notes["requires_migration"] and notes["migration_steps"]:
            mark_transaction("migration", target_commit=target.commit_sha)
            await notify("Running %d migration step(s)..." % len(notes["migration_steps"]))
            steps = notes["migration_steps"]
            for step_number, step in enumerate(steps, start=1):
                await notify(
                    "Running migration step %d/%d..." % (step_number, len(steps))
                )
                try:
                    # Use shlex.split so commands with quoted arguments or
                    # multi-word flags (e.g. `python -m pip install foo`,
                    # `mv "old name" new`) tokenize correctly. Plain
                    # str.split would have broken the quoting.
                    try:
                        argv = shlex.split(step)
                    except ValueError:
                        return await rollback_failure(
                            "Unparseable migration step %d (command withheld)"
                            % step_number
                        )
                    if not argv:
                        continue
                    proc, tracked = await _spawn_update_child(
                        *argv,
                        owner="migration",
                        cwd=str(PROJECT_ROOT),
                        env=_migration_child_env(),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=sys.platform != "win32",
                    )
                    stdout, stderr = await _communicate_update_child(
                        proc,
                        timeout=120,
                        owner="migration",
                        already_tracked=tracked,
                    )
                    if proc.returncode != 0:
                        error = stderr.decode().strip() or stdout.decode().strip()
                        return await rollback_failure(
                            "Migration step failed (step %d):\n%s" % (
                                step_number,
                                _safe_diagnostic_tail(error),
                            )
                        )
                except asyncio.TimeoutError:
                    return await rollback_failure(
                        "Migration step %d timed out" % step_number
                    )

        # 6. Always reinstall deps. A silently-failed install was the root
        # cause of the v0.12.0 "No module named 'PIL'" boot crash, so we
        # now check the return code, preserve only a bounded/redacted failure
        # tail, roll back on non-zero, and allow time for wheel builds.
        await notify("Installing dependencies...")
        mark_transaction("dependencies", target_commit=target.commit_sha)
        venv_bin = "Scripts" if sys.platform == "win32" else "bin"
        pip_name = "pip.exe" if sys.platform == "win32" else "pip"
        pip_path = PROJECT_ROOT / ".venv" / venv_bin / pip_name
        if not pip_path.exists():
            return await rollback_failure("venv pip not found at %s" % pip_path)
        try:
            runtime_lock = dependency_lock_path(PROJECT_ROOT, kind="runtime")
        except DependencyLockError as exc:
            return await rollback_failure(
                "runtime dependency lock unavailable: %s" % exc
            )

        deps_proc, deps_tracked = await _spawn_update_child(
            str(pip_path),
            "install", "--require-hashes", "-r", str(runtime_lock),
            owner="pip",
            cwd=str(PROJECT_ROOT),
            env=_scrubbed_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
        try:
            pip_stdout, pip_stderr = await _communicate_update_child(
                deps_proc,
                timeout=600,
                owner="pip",
                already_tracked=deps_tracked,
            )
        except asyncio.TimeoutError:
            return await rollback_failure("pip install timed out after 600s")

        pip_out_text = pip_stdout.decode(errors="replace")
        pip_err_text = pip_stderr.decode(errors="replace")
        if deps_proc.returncode != 0:
            tail_lines = (pip_err_text or pip_out_text).strip().splitlines()[-8:]
            tail_str = _safe_diagnostic_tail("\n".join(tail_lines))
            return await rollback_failure(
                "pip install returned %d:\n%s" % (deps_proc.returncode, tail_str)
            )

        # Refresh the bootstrap marker so the next start-up does not
        # redundantly re-run pip for the same requirements.txt.
        try:
            req_file = PROJECT_ROOT / "bot" / "requirements.txt"
            marker = PROJECT_ROOT / ".venv" / ".robyx_deps_hash"
            _write_dependency_marker(
                marker,
                dependency_fingerprint(req_file, runtime_lock),
            )
        except Exception as e:
            log.warning("Could not refresh bootstrap marker: %s", e)

        # 6.5 Smoke test: import the new code in a fresh subprocess to
        # catch import-time errors (e.g. broken pip-resolved dependency
        # graph, syntax error from a partial commit, missing migration
        # constant). pip exit 0 isn't enough — a successful resolve can
        # still leave the runtime broken. On failure we roll back the
        # code (reset to the pre-update commit) AND restore data/ from the
        # snapshot so a partially-applied migration doesn't leave the
        # next boot reading half-mutated state.
        await notify("Smoke-testing imports...")
        mark_transaction("smoke-test", target_commit=target.commit_sha)
        smoke_ok, smoke_err = await _post_update_smoke_test()
        if not smoke_ok:
            await notify("Smoke test failed; rolling back: %s" % smoke_err)
            return await rollback_failure("Smoke test failed: %s" % smoke_err)

        # 7. Pop stash if we had one. v0.28.2 hotfix: on conflict the
        # stash pop leaves raw ``<<<<<<<`` markers in the working tree —
        # restarting the bot at that point produces a SyntaxError
        # crashloop (the v0.28.0 Linux incident). Catch the conflict
        # explicitly and roll back code + data/ instead of restarting
        # into a broken file. Git preserves the stash on conflict, so
        # the user's WIP is not lost — they can resolve at their
        # leisure.
        if has_stash:
            try:
                await _safe_stash_pop(strict=True)
            except StashPopConflict as exc:
                await notify(
                    "Stash-pop conflict on %s; rolling back: %s"
                    % (", ".join(exc.unmerged_paths) or "(unknown)", exc),
                )
                # NOTE: do NOT re-pop the stash here. Git preserved it
                # automatically on conflict; the operator resolves
                # manually when they reach the machine.
                return await rollback_failure(
                    "Stash-pop conflict on %s. "
                    "Stash preserved at stash@{0}. Resolve manually: "
                    "edit the conflicted files to remove "
                    "<<<<<<</=======/>>>>>>> markers, `git add` them, "
                    "then `git stash drop` and re-trigger the update."
                    % (", ".join(exc.unmerged_paths) or "(unknown)"),
                    restore_stash=False,
                )
            stash_pending = False

            # 7.1 Belt-and-braces — verify every Python file in the
            # working tree parses cleanly after the stash pop. The
            # post-update smoke test at step 5.5 ran BEFORE the pop, so
            # a pop that mutated a file without producing unmerged
            # paths (rare but possible — e.g. a clean apply that
            # introduces a typo) would otherwise restart the bot into a
            # SyntaxError. This is a cheap parse-only check; the full
            # smoke test (subprocess boot of bot.py) already ran.
            syntax_ok, syntax_err = _check_python_syntax_in_repo()
            if not syntax_ok:
                await notify(
                    "Post-stash syntax check failed; rolling back: %s"
                    % syntax_err,
                )
                # Re-stash the popped changes so they aren't lost when we
                # roll back the code. The reflog keeps the original stash
                # accessible too, but a fresh `stash push` is the
                # cleanest recovery shape for the operator.
                restash = await _git("stash", "push", "-m",
                    "robyx auto-update rollback (post-pop syntax failure)",
                    check=False,
                )
                if restash.returncode != 0:
                    critical = (
                        "Python syntax check failed after stash pop (%s), and "
                        "the updater could not re-stash local changes. Code/data "
                        "were left untouched to avoid losing WIP; manual recovery "
                        "is required." % syntax_err
                    )
                    log.critical(critical)
                    record_failure(critical)
                    return False, critical
                stash_pending = True
                log.info("Re-stashed user changes into stash@{0} before rollback")
                return await rollback_failure(
                    "Python syntax check failed after stash pop: %s" % syntax_err,
                    restore_stash=False,
                )

        # The restored stash may have changed VERSION without creating a merge
        # conflict. Re-check target consistency before recording success. If it
        # fails, preserve the now-restored WIP before rolling back.
        final_ok, final_error = await _verify_installed_target(target)
        if not final_ok:
            if has_stash and not stash_pending:
                restash = await _git(
                    "stash", "push", "-m",
                    "robyx auto-update rollback (post-pop target mismatch)",
                    check=False,
                )
                if restash.returncode != 0:
                    critical = (
                        "Final target verification failed (%s), and local changes "
                        "could not be re-stashed. Code/data were left untouched to "
                        "avoid losing WIP; manual recovery is required." % final_error
                    )
                    log.critical(critical)
                    record_failure(critical)
                    return False, critical
                stash_pending = True
            return await rollback_failure(
                "Final target verification failed: %s" % final_error,
                restore_stash=False,
            )

        # 7.5 Invalidate AI-CLI sessions for any agent whose system prompt
        # or per-agent brief was changed by this update. See the module
        # docstring of session_lifecycle for the rationale; the short
        # version is that --resume sessions ignore the new system prompt,
        # so we must force a fresh session for affected agents. We
        # compute the diff between the pre-update commit captured above
        # and the new HEAD, then hand the changed paths to the
        # AgentManager-aware helper. Routing through manager.reset_sessions
        # (instead of mutating state.json directly) is critical: the
        # running bot's AgentManager holds the agent state in memory and
        # would silently overwrite a direct file mutation on its next
        # save_state() call. Failures here are logged but never block
        # the update — the restart still happens.
        if manager is not None:
            try:
                diff = await _git(
                    "diff", "--name-only", pre_pull_sha, "HEAD",
                    check=False,
                )
                if diff.returncode == 0:
                    changed_paths = [
                        line.strip()
                        for line in diff.stdout.splitlines()
                        if line.strip()
                    ]
                    if changed_paths:
                        reset = invalidate_sessions_via_manager(
                            manager, changed_paths,
                        )
                        if reset:
                            await notify(
                                "Reset AI sessions for %d agent(s): %s"
                                % (len(reset), ", ".join(reset))
                            )
                else:
                    log.warning(
                        "git diff %s..HEAD failed: %s",
                        pre_pull_sha, diff.stderr.strip() or diff.stdout.strip(),
                    )
            except Exception as e:
                log.warning(
                    "Session invalidation step raised — continuing: %s", e,
                    exc_info=True,
                )
        else:
            log.warning(
                "apply_update called without manager — skipping session invalidation"
            )

        # 8. Record success only after HEAD and VERSION have both been proven
        # consistent with the requested tag. Include the commit for auditability.
        state = _load_state()
        now = datetime.now(timezone.utc).isoformat()
        state["last_update"] = now
        state["update_history"].append({
            "version": version,
            "from_version": current,
            "tag": target.tag,
            "commit": target.commit_sha,
            "date": now,
            "status": "ok",
        })
        _save_state(state)

        return True, version

    except BaseException as e:
        safe_error = _safe_diagnostic_tail(e, max_chars=4000)
        log.error("Update failed with exception: %s", safe_error)
        rollback_task = asyncio.create_task(rollback_failure(safe_error))
        rollback_result = await _await_cleanup_uninterruptibly(rollback_task)
        if not isinstance(e, Exception):
            # Cancellation/SystemExit semantics remain visible to the caller,
            # but only after code, data, and child cleanup has completed.
            raise
        return rollback_result


def restart_service():
    """Restart the Robyx service via the platform's service manager."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(
                ["launchctl", "kickstart", "-k",
                 "gui/%d/com.robyx.bot" % _get_uid()],
                start_new_session=True,
            )
        elif system == "Linux":
            subprocess.Popen(
                ["systemctl", "--user", "restart", "robyx"],
                start_new_session=True,
            )
        elif system == "Windows":
            subprocess.Popen(
                ["powershell", "-Command",
                 "Stop-ScheduledTask -TaskName Robyx; Start-ScheduledTask -TaskName Robyx"],
                start_new_session=True,
            )
        else:
            log.warning("Unsupported platform for auto-restart: %s", system)
    except Exception as e:
        log.error("Failed to restart service: %s", e)


def _get_uid() -> int:
    """Get current user UID (macOS/Linux)."""
    import os
    return os.getuid()
