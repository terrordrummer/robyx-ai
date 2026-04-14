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
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import Version

from config import (
    DATA_DIR,
    PROJECT_ROOT,
    RELEASES_DIR,
    UPDATES_STATE_FILE,
    VERSION_FILE,
)
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


# ── Pre-update data backup + post-update smoke test ──

BACKUPS_DIR_NAME = "backups"
SNAPSHOT_RETENTION = 3
SNAPSHOT_PREFIX = "pre-update-"


def _snapshot_data_dir(from_version: str, to_version: str) -> Path | None:
    """Tar+gzip ``DATA_DIR`` to a versioned snapshot under ``DATA_DIR/backups/``.

    A failed update can then roll back not only the code (via the
    previous git tag) but also any data mutated by a migration that ran
    before the failure was detected. The snapshot excludes the
    ``backups/`` subdirectory itself to avoid runaway recursive growth
    across successive updates.

    Returns the snapshot path, or ``None`` on failure (snapshot failure
    is logged but never blocks the update — better to attempt an
    unprotected update than to refuse to update because backup is
    flaky).
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

    def _filter(tarinfo):
        rel = tarinfo.name.lstrip("./")
        if rel == BACKUPS_DIR_NAME or rel.startswith(BACKUPS_DIR_NAME + "/"):
            return None
        return tarinfo

    try:
        with tarfile.open(str(out), "w:gz") as tf:
            tf.add(str(DATA_DIR), arcname=".", filter=_filter)
    except (OSError, tarfile.TarError) as e:
        log.warning("Snapshot of %s failed: %s", DATA_DIR, e)
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    log.info("Created data/ snapshot: %s", out)
    _prune_old_snapshots(backups)
    return out


def _prune_old_snapshots(backups: Path, keep: int = SNAPSHOT_RETENTION) -> None:
    """Keep only the most recent *keep* snapshots; best-effort prune."""
    try:
        snaps = sorted(
            backups.glob(SNAPSHOT_PREFIX + "*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    if len(snaps) <= keep:
        return
    for p in snaps[:-keep]:
        try:
            p.unlink()
            log.debug("Pruned old snapshot: %s", p)
        except OSError as e:
            log.debug("Could not prune %s: %s", p, e)


def _restore_data_dir(snapshot: Path) -> bool:
    """Extract *snapshot* back into ``DATA_DIR`` overwriting any files
    mutated since the snapshot was taken.

    The snapshot was created with ``backups/`` excluded, so existing
    snapshots in ``DATA_DIR/backups/`` are untouched by the restore.
    Returns ``True`` on success.
    """
    if not snapshot.exists():
        log.error("Cannot restore: snapshot %s missing", snapshot)
        return False
    try:
        with tarfile.open(str(snapshot), "r:gz") as tf:
            tf.extractall(str(DATA_DIR))
    except (OSError, tarfile.TarError) as e:
        log.error("Restore from %s failed: %s", snapshot, e)
        return False
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
    proc = await asyncio.create_subprocess_exec(
        str(py), str(bot_py), "--smoke-test",
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "smoke test timed out after 60s"

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or stdout.decode(errors="replace")).strip()
        return False, "bot.py --smoke-test exited %d: %s" % (proc.returncode, err[-500:])
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
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
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


# ── v0.16 personal-data migration (pre-pull) ──


def migrate_personal_data_to_data_dir() -> list[str]:
    """v0.16 pre-pull migration: copy tracked runtime files to ``data/``.

    Before v0.16, Robyx shipped personal runtime files committed at the
    repo root (``tasks.md``, ``specialists.md``, ``agents/<name>.md``,
    ``specialists/<name>.md``). v0.16 moves these under ``data/`` which is
    gitignored. On the user's live runtime install, the updater must copy
    these files into ``data/`` **before** the ``git pull`` removes them
    from the working tree — otherwise the pull drops them and the fleet
    boots with an empty state.

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


async def apply_update(version: str, notify_fn=None, manager=None) -> tuple[bool, str]:
    """Apply an update to the given version.

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
    current = get_current_version()

    async def notify(msg):
        if notify_fn:
            await notify_fn(msg)
        log.info(msg)

    # 1. Stash local changes
    stash_result = await _git("stash", "--include-untracked", check=False)
    has_stash = "No local changes" not in stash_result.stdout

    # 1.5 Snapshot data/ so a failed migration / smoke test can be rolled
    # back. Snapshot failure is logged but never blocks the update.
    snapshot = _snapshot_data_dir(current, version)
    if snapshot is not None:
        await notify("Created data snapshot: %s" % snapshot.name)

    # Capture the pre-pull commit so we can compute, after the pull, which
    # files this update actually changed. The diff drives the per-agent
    # session invalidation in step 7 — without it, agents whose AI-CLI
    # sessions pre-existed a prompt/brief change would keep running under
    # the stale system prompt indefinitely (Claude Code CLI bakes the
    # system prompt at session creation and ignores --append-system-prompt
    # on --resume). Failure to capture is not fatal: we just skip the
    # invalidation step and log it.
    pre_pull_sha: str | None = None
    try:
        pre_pull = await _git("rev-parse", "HEAD", check=False)
        if pre_pull.returncode == 0:
            pre_pull_sha = pre_pull.stdout.strip() or None
    except Exception as e:
        log.warning("Could not capture pre-pull HEAD: %s", e)

    # v0.16+: migrate personal runtime files (tasks.md, specialists.md,
    # agents/*.md, specialists/*.md) into data/ BEFORE the pull. Starting
    # with v0.16 these files are no longer tracked; a naive pull would
    # delete them from the working tree and take the user's fleet down.
    # The helper is idempotent, so running it on every apply_update is
    # safe — it only copies files that still exist at the repo root and
    # are not yet present under data/.
    try:
        moved = migrate_personal_data_to_data_dir()
        if moved:
            await notify(
                "Migrated %d file(s) to data/: %s"
                % (len(moved), ", ".join(moved))
            )
    except Exception as e:
        log.warning("Personal-data migration raised — continuing: %s", e, exc_info=True)

    try:
        # 2. Ensure we're on main before pulling. A previous rollback
        # leaves HEAD detached at a tag (``git checkout v<old>`` in the
        # failure path below); from that state ``git pull --ff-only``
        # aborts with "You are not currently on a branch". Re-attach to
        # main so subsequent updates can recover without manual
        # intervention. Local work was already stashed at step 1.
        head_check = await _git("symbolic-ref", "--quiet", "HEAD", check=False)
        if head_check.returncode != 0:
            await notify("Detached HEAD detected — reattaching to main")
            attach = await _git("checkout", "main", check=False)
            if attach.returncode != 0:
                error = attach.stderr.strip() or attach.stdout.strip()
                if has_stash:
                    await _git("stash", "pop", check=False)
                return False, "could not reattach to main: %s" % error

        # 3. Fast-forward pull only
        await notify("Pulling latest changes...")
        pull = await _git("pull", "--ff-only", check=False)
        if pull.returncode != 0:
            error = pull.stderr.strip() or pull.stdout.strip()
            if has_stash:
                await _git("stash", "pop", check=False)
            return False, "git pull --ff-only failed: %s" % error

        # 3. Read release notes from the now-available local file
        notes_file = RELEASES_DIR / ("%s.md" % version)
        notes = None
        if notes_file.exists():
            notes = _parse_release_notes(notes_file.read_text())

        # 4. Run migration steps if needed
        if notes and notes["requires_migration"] and notes["migration_steps"]:
            await notify("Running %d migration step(s)..." % len(notes["migration_steps"]))
            for step in notes["migration_steps"]:
                await notify("  $ %s" % step)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *step.split(),
                        cwd=str(PROJECT_ROOT),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                    if proc.returncode != 0:
                        error = stderr.decode().strip() or stdout.decode().strip()
                        # Rollback: previous version tag + restore data/.
                        await _git("checkout", "v" + current, check=False)
                        if snapshot is not None:
                            _restore_data_dir(snapshot)
                        if has_stash:
                            await _git("stash", "pop", check=False)
                        return False, "Migration step failed: `%s`\n%s" % (step, error)
                except asyncio.TimeoutError:
                    await _git("checkout", "v" + current, check=False)
                    if snapshot is not None:
                        _restore_data_dir(snapshot)
                    if has_stash:
                        await _git("stash", "pop", check=False)
                    return False, "Migration step timed out: `%s`" % step

        # 5. Always reinstall deps. A silently-failed install was the root
        # cause of the v0.12.0 "No module named 'PIL'" boot crash, so we
        # now (a) keep pip verbose, (b) check the return code, (c) log the
        # output, (d) roll back and fail the update if pip exits non-zero,
        # (e) use a longer timeout to accommodate wheel builds.
        await notify("Installing dependencies...")
        venv_bin = "Scripts" if sys.platform == "win32" else "bin"
        pip_name = "pip.exe" if sys.platform == "win32" else "pip"
        pip_path = PROJECT_ROOT / ".venv" / venv_bin / pip_name
        if not pip_path.exists():
            await _git("checkout", "v" + current, check=False)
            if has_stash:
                await _git("stash", "pop", check=False)
            return False, "venv pip not found at %s" % pip_path

        deps_proc = await asyncio.create_subprocess_exec(
            str(pip_path),
            "install", "-r", str(PROJECT_ROOT / "bot" / "requirements.txt"),
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            pip_stdout, pip_stderr = await asyncio.wait_for(
                deps_proc.communicate(), timeout=600,
            )
        except asyncio.TimeoutError:
            deps_proc.kill()
            await _git("checkout", "v" + current, check=False)
            if has_stash:
                await _git("stash", "pop", check=False)
            return False, "pip install timed out after 600s"

        pip_out_text = pip_stdout.decode(errors="replace")
        pip_err_text = pip_stderr.decode(errors="replace")
        if pip_out_text.strip():
            log.info("pip install stdout:\n%s", pip_out_text.strip())
        if pip_err_text.strip():
            log.info("pip install stderr:\n%s", pip_err_text.strip())

        if deps_proc.returncode != 0:
            await _git("checkout", "v" + current, check=False)
            if has_stash:
                await _git("stash", "pop", check=False)
            tail_lines = (pip_err_text or pip_out_text).strip().splitlines()[-8:]
            tail_str = "\n".join(tail_lines)
            return False, "pip install returned %d:\n%s" % (deps_proc.returncode, tail_str)

        # Refresh the bootstrap marker so the next start-up does not
        # redundantly re-run pip for the same requirements.txt.
        try:
            import hashlib
            req_file = PROJECT_ROOT / "bot" / "requirements.txt"
            marker = PROJECT_ROOT / ".venv" / ".robyx_deps_hash"
            marker.write_text(hashlib.sha1(req_file.read_bytes()).hexdigest())
        except Exception as e:
            log.warning("Could not refresh bootstrap marker: %s", e)

        # 5.5 Smoke test: import the new code in a fresh subprocess to
        # catch import-time errors (e.g. broken pip-resolved dependency
        # graph, syntax error from a partial commit, missing migration
        # constant). pip exit 0 isn't enough — a successful resolve can
        # still leave the runtime broken. On failure we roll back the
        # code (checkout previous tag) AND restore data/ from the
        # snapshot so a partially-applied migration doesn't leave the
        # next boot reading half-mutated state.
        await notify("Smoke-testing imports...")
        smoke_ok, smoke_err = await _post_update_smoke_test()
        if not smoke_ok:
            await notify("Smoke test failed; rolling back: %s" % smoke_err)
            await _git("checkout", "v" + current, check=False)
            if snapshot is not None:
                _restore_data_dir(snapshot)
            if has_stash:
                await _git("stash", "pop", check=False)
            return False, "Smoke test failed: %s" % smoke_err

        # 6. Pop stash if we had one
        if has_stash:
            await _git("stash", "pop", check=False)

        # 6.5 Invalidate AI-CLI sessions for any agent whose system prompt
        # or per-agent brief was changed by this update. See the module
        # docstring of session_lifecycle for the rationale; the short
        # version is that --resume sessions ignore the new system prompt,
        # so we must force a fresh session for affected agents. We
        # compute the diff between the pre-pull commit captured above
        # and the new HEAD, then hand the changed paths to the
        # AgentManager-aware helper. Routing through manager.reset_sessions
        # (instead of mutating state.json directly) is critical: the
        # running bot's AgentManager holds the agent state in memory and
        # would silently overwrite a direct file mutation on its next
        # save_state() call. Failures here are logged but never block
        # the update — the restart still happens.
        if pre_pull_sha and manager is not None:
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
        elif pre_pull_sha is None:
            log.info(
                "No pre-pull SHA captured — skipping session invalidation"
            )
        elif manager is None:
            log.warning(
                "apply_update called without manager — skipping session invalidation"
            )

        # 7. Record success
        state = _load_state()
        now = datetime.now(timezone.utc).isoformat()
        state["last_update"] = now
        state["update_history"].append({
            "version": version,
            "from_version": current,
            "date": now,
            "status": "ok",
        })
        _save_state(state)

        return True, version

    except Exception as e:
        # Catastrophic rollback: code + data.
        log.error("Update failed with exception: %s", e, exc_info=True)
        await _git("checkout", "v" + current, check=False)
        if snapshot is not None:
            _restore_data_dir(snapshot)
        if has_stash:
            await _git("stash", "pop", check=False)

        state = _load_state()
        state["update_history"].append({
            "version": version,
            "from_version": current,
            "date": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "error": str(e),
        })
        _save_state(state)

        return False, str(e)


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
