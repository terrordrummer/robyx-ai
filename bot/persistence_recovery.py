"""Fail-closed recovery helpers for JSON runtime state.

Updater snapshots contain paths relative to ``DATA_DIR``.  This module restores
one JSON file at a time, validates its top-level shape, and atomically installs
it without extracting the rest of the archive.  A durable sidecar marker makes
the quarantine/install sequence resumable if the process exits between those
steps.
"""

from __future__ import annotations

import json
import logging
import os
import tarfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BACKUPS_DIR_NAME = "backups"
SNAPSHOT_PREFIX = "pre-update-"
MAX_RECOVERY_FILE_BYTES = 32 * 1024 * 1024

JsonValidator = Callable[[Any], bool]


class PersistenceUnavailableError(RuntimeError):
    """The live JSON file is corrupt and no verified copy is available."""


@dataclass(frozen=True)
class JsonLoadResult:
    """Result of a JSON load that preserves missing/recovered distinctions."""

    value: Any | None
    status: str  # missing | valid | recovered
    snapshot: Path | None = None


def recovery_marker_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".recovery-pending")


def unavailable_marker_path(path: Path) -> Path:
    """Sidecar that prevents a stale in-memory writer from retrying.

    It is installed when a write guard had to recover the live file.  A later
    authoritative load clears it; another blind write in the same lifecycle
    remains blocked.
    """
    return path.with_suffix(path.suffix + ".unavailable")


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync for rename/unlink durability."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, content: bytes, *, suffix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + suffix)
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        # If the write or rename failed, a partial temporary copy must never be
        # mistaken for a valid live file on the next boot.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_recovery_marker(path: Path, kind: str) -> None:
    marker = recovery_marker_path(path)
    payload = json.dumps({
        "kind": kind,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")
    _atomic_write_bytes(marker, payload, suffix=".tmp")


def _write_unavailable_marker(path: Path, kind: str) -> None:
    marker = unavailable_marker_path(path)
    payload = json.dumps({
        "kind": kind,
        "reason": "live state recovered during write; authoritative reload required",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")
    _atomic_write_bytes(marker, payload, suffix=".tmp")


def _clear_marker(marker: Path, logger: logging.Logger) -> None:
    try:
        marker.unlink(missing_ok=True)
        _fsync_directory(marker.parent)
    except OSError as exc:
        # A stale marker is harmless: the next valid load removes it again.
        logger.warning("Could not remove stale recovery marker %s: %s", marker, exc)


def _clear_recovery_marker(path: Path, logger: logging.Logger) -> None:
    _clear_marker(recovery_marker_path(path), logger)


def _decode_valid_json(content: bytes, validator: JsonValidator) -> Any:
    if len(content) > MAX_RECOVERY_FILE_BYTES:
        raise ValueError("JSON file exceeds recovery size limit")
    value = json.loads(content.decode("utf-8"))
    if not validator(value):
        raise ValueError("unexpected top-level JSON shape")
    return value


def _read_live_json(path: Path, validator: JsonValidator) -> Any:
    content = path.read_bytes()
    return _decode_valid_json(content, validator)


def _quarantine(path: Path, *, reason: str, logger: logging.Logger) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = path.with_suffix(
        path.suffix + ".corrupt-" + timestamp + "-" + uuid.uuid4().hex[:8]
    )
    try:
        os.replace(path, quarantine)
        _fsync_directory(path.parent)
    except OSError as exc:
        logger.critical(
            "Failed to quarantine corrupt %s: %s; refusing recovery/write",
            path,
            exc,
        )
        raise PersistenceUnavailableError(
            "%s is corrupt and could not be quarantined" % path
        ) from exc
    logger.critical(
        "Quarantined corrupt runtime file %s as %s (%s)",
        path,
        quarantine,
        reason,
    )
    return quarantine


def _snapshot_candidates(backups_dir: Path) -> list[Path]:
    try:
        return sorted(
            backups_dir.glob(SNAPSHOT_PREFIX + "*.tar.gz"),
            key=lambda candidate: (candidate.stat().st_mtime, candidate.name),
            reverse=True,
        )
    except OSError as exc:
        raise PersistenceUnavailableError(
            "cannot list recovery snapshots in %s" % backups_dir
        ) from exc


def _read_snapshot_member(
    snapshot: Path,
    member_names: tuple[str, str],
    validator: JsonValidator,
) -> bytes | None:
    with tarfile.open(snapshot, "r:gz") as archive:
        member = None
        for name in member_names:
            try:
                member = archive.getmember(name)
                break
            except KeyError:
                continue
        if member is None or not member.isfile() or member.issym() or member.islnk():
            return None
        if member.size > MAX_RECOVERY_FILE_BYTES:
            raise ValueError("snapshot member exceeds recovery size limit")
        extracted = archive.extractfile(member)
        if extracted is None:
            return None
        content = extracted.read(MAX_RECOVERY_FILE_BYTES + 1)
    _decode_valid_json(content, validator)
    return content


def _recover_from_snapshots(
    path: Path,
    *,
    data_dir: Path,
    validator: JsonValidator,
    kind: str,
    logger: logging.Logger,
) -> Path | None:
    try:
        relative = path.resolve().relative_to(data_dir.resolve())
    except ValueError:
        logger.critical(
            "Cannot recover %s %s: target is outside data directory %s",
            kind,
            path,
            data_dir,
        )
        return None

    backups_dir = data_dir / BACKUPS_DIR_NAME
    if not backups_dir.is_dir():
        logger.critical(
            "Cannot recover %s %s: no updater snapshots at %s",
            kind,
            path,
            backups_dir,
        )
        return None

    relative_name = relative.as_posix()
    member_names = ("./" + relative_name, relative_name)
    try:
        snapshots = _snapshot_candidates(backups_dir)
    except PersistenceUnavailableError as exc:
        logger.critical("Cannot recover %s %s: %s", kind, path, exc)
        return None

    for snapshot in snapshots:
        try:
            content = _read_snapshot_member(snapshot, member_names, validator)
        except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Snapshot %s has no usable %s copy (%s); trying older snapshot",
                snapshot.name,
                relative_name,
                exc,
            )
            continue
        if content is None:
            continue
        try:
            _atomic_write_bytes(path, content, suffix=".recover-tmp")
        except OSError as exc:
            logger.critical(
                "Verified %s recovery from %s but atomic install failed: %s",
                kind,
                snapshot.name,
                exc,
            )
            return None
        logger.critical(
            "Recovered %s %s from snapshot %s; writes since that snapshot "
            "may require operator reconciliation",
            kind,
            path,
            snapshot.name,
        )
        return snapshot

    logger.critical(
        "No usable snapshot can recover %s %s; mutations remain disabled",
        kind,
        path,
    )
    return None


def load_json_with_recovery(
    path: Path,
    *,
    data_dir: Path,
    validator: JsonValidator,
    kind: str,
    logger: logging.Logger,
) -> JsonLoadResult:
    """Load JSON, quarantining/recovering corruption and failing closed.

    A missing file is a normal result unless a recovery marker is present.  A
    marker with no live file means an earlier process was interrupted after
    quarantine, so recovery resumes instead of treating the file as new.
    """
    path = Path(path)
    data_dir = Path(data_dir)
    marker = recovery_marker_path(path)
    unavailable = unavailable_marker_path(path)

    if not path.exists():
        if not marker.exists():
            if unavailable.exists():
                raise PersistenceUnavailableError(
                    "%s %s is unavailable and has no live file" % (kind, path)
                )
            return JsonLoadResult(None, "missing")
        snapshot = _recover_from_snapshots(
            path,
            data_dir=data_dir,
            validator=validator,
            kind=kind,
            logger=logger,
        )
        if snapshot is None:
            raise PersistenceUnavailableError(
                "%s %s recovery is incomplete and no valid snapshot is available"
                % (kind, path)
            )
        _clear_recovery_marker(path, logger)
        result = JsonLoadResult(_read_live_json(path, validator), "recovered", snapshot)
        _clear_marker(unavailable, logger)
        return result

    try:
        value = _read_live_json(path, validator)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        try:
            _write_recovery_marker(path, kind)
        except OSError as marker_exc:
            logger.critical(
                "Cannot begin recovery for corrupt %s %s: %s",
                kind,
                path,
                marker_exc,
            )
            raise PersistenceUnavailableError(
                "cannot durably mark %s recovery for %s" % (kind, path)
            ) from marker_exc
        _quarantine(path, reason=type(exc).__name__, logger=logger)
        snapshot = _recover_from_snapshots(
            path,
            data_dir=data_dir,
            validator=validator,
            kind=kind,
            logger=logger,
        )
        if snapshot is None:
            raise PersistenceUnavailableError(
                "%s %s is corrupt and no valid snapshot is available" % (kind, path)
            ) from exc
        _clear_recovery_marker(path, logger)
        result = JsonLoadResult(_read_live_json(path, validator), "recovered", snapshot)
        _clear_marker(unavailable, logger)
        return result

    if marker.exists():
        # The prior process may have crashed after installing valid bytes but
        # before clearing the durable transaction marker.
        _clear_recovery_marker(path, logger)
    if unavailable.exists():
        # This is an explicit authoritative read, so the caller can safely
        # rebuild its state before attempting another mutation.
        _clear_marker(unavailable, logger)
    return JsonLoadResult(value, "valid")


def guard_json_write(
    path: Path,
    *,
    data_dir: Path,
    validator: JsonValidator,
    kind: str,
    logger: logging.Logger,
) -> None:
    """Refuse to overwrite corrupt or incompletely-recovered live data.

    If corruption is discovered here, recovery is attempted for preservation,
    but this write still fails.  The caller's object may have been derived from
    stale bytes, so it must reload the recovered state before retrying.
    """
    path = Path(path)
    unavailable = unavailable_marker_path(path)
    if unavailable.exists():
        raise PersistenceUnavailableError(
            "%s %s requires an authoritative reload before writing" % (kind, path)
        )
    marker_existed = recovery_marker_path(path).exists()
    if not path.exists() and not marker_existed:
        return
    try:
        result = load_json_with_recovery(
            path,
            data_dir=Path(data_dir),
            validator=validator,
            kind=kind,
            logger=logger,
        )
    except PersistenceUnavailableError:
        raise
    if marker_existed or result.status == "recovered":
        try:
            _write_unavailable_marker(path, kind)
        except OSError as exc:
            logger.critical(
                "Could not persist unavailable marker for %s %s: %s",
                kind,
                path,
                exc,
            )
            # Keep the older recovery marker contract as a fallback barrier.
            # A retry will observe it and fail again instead of overwriting the
            # verified snapshot with stale caller state.
            try:
                _write_recovery_marker(path, kind)
            except OSError as fallback_exc:
                logger.critical(
                    "Could not persist fallback recovery marker for %s %s: %s",
                    kind,
                    path,
                    fallback_exc,
                )
        raise PersistenceUnavailableError(
            "%s %s was recovered during a write; reload before retrying"
            % (kind, path)
        )
