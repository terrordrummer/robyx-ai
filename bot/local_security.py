"""Local filesystem hardening for Robyx runtime data.

The module is deliberately stdlib-only: it runs before ``config`` and the
optional runtime dependencies are imported.  POSIX installations use exact
owner-only modes (directories ``0700``, regular files ``0600``).  Windows has
no portable ACL equivalent in the Python stdlib, so the same calls are safe,
documented no-ops there.

This is a boot/upgrade safety net rather than a versioned data migration.  It
is idempotent, does not follow symlinks, and therefore also repairs manually
updated installations whose migration tracker is already at the latest
version.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_UMASK = 0o077


@dataclass
class HardeningReport:
    """Summary returned to boot, installers, and tests."""

    changed_files: int = 0
    changed_directories: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class PermissionHardeningError(RuntimeError):
    """Owner-only runtime permissions could not be established."""


def require_hardening_success(report: HardeningReport) -> None:
    """Fail closed without embedding paths or file contents in the error."""
    if not report.ok:
        raise PermissionHardeningError(
            "Robyx refused to start because local runtime permissions could not be secured"
        )


def supports_posix_permissions(*, platform_name: str | None = None) -> bool:
    """Return whether exact owner/group/other mode enforcement is available."""
    return (platform_name or os.name) == "posix"


def establish_private_umask(*, platform_name: str | None = None) -> int | None:
    """Make subsequently-created runtime files owner-only on POSIX.

    Called only from the executable entrypoint, never at import time, so test
    runners and applications importing ``bot`` do not inherit a changed umask.
    The returned previous mask is useful to tightly scoped callers/tests.
    """
    if not supports_posix_permissions(platform_name=platform_name):
        return None
    return os.umask(PRIVATE_UMASK)


def _entries_without_following(
    root: Path,
    *,
    on_error: Callable[[str], None],
) -> Iterator[Path]:
    """Yield descendants without traversing symlinked directories."""
    pending = [root]
    while pending:
        current = pending.pop()
        yield current
        try:
            info = current.lstat()
        except OSError as exc:
            on_error("could not inspect %s: %s" % (current, exc))
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        try:
            with os.scandir(current) as entries:
                children = [Path(entry.path) for entry in entries]
        except OSError as exc:
            on_error("could not traverse %s: %s" % (current, exc))
            continue
        pending.extend(reversed(sorted(children, key=lambda item: item.name)))


def _harden_one(
    path: Path,
    report: HardeningReport,
    *,
    strict: bool = False,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        report.errors.append("could not inspect %s: %s" % (path, exc))
        return

    if stat.S_ISLNK(info.st_mode):
        message = "refused to follow symlink %s" % path
        if strict:
            report.errors.append(message)
        else:
            report.skipped.append(message)
        return
    if stat.S_ISDIR(info.st_mode):
        wanted = PRIVATE_DIRECTORY_MODE
        counter = "changed_directories"
    elif stat.S_ISREG(info.st_mode):
        wanted = PRIVATE_FILE_MODE
        counter = "changed_files"
    else:
        message = "refused non-regular runtime path %s" % path
        if strict:
            report.errors.append(message)
        else:
            report.skipped.append(message)
        return

    if stat.S_IMODE(info.st_mode) == wanted:
        return
    try:
        path.chmod(wanted, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        report.errors.append("could not harden %s: %s" % (path, exc))
        return
    setattr(report, counter, getattr(report, counter) + 1)


def ensure_private_directory(path: Path) -> None:
    """Create a private POSIX directory and repair an existing one's mode."""
    if path.is_symlink():
        raise OSError("private runtime directory must not be a symlink: %s" % path)
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    if supports_posix_permissions():
        path.chmod(PRIVATE_DIRECTORY_MODE)


def harden_runtime_permissions(
    project_root: Path,
    *,
    platform_name: str | None = None,
) -> HardeningReport:
    """Harden existing Robyx credentials and runtime state in place.

    ``project_root`` itself is deliberately not changed.  Only ``.env``,
    ``bot.log`` (including rotated siblings), and the complete ``data/`` tree
    are runtime-private.  Symlinks are reported and never followed.
    """
    report = HardeningReport()
    if not supports_posix_permissions(platform_name=platform_name):
        return report

    root = Path(project_root)
    env_file = root / ".env"
    if env_file.exists() or env_file.is_symlink():
        _harden_one(env_file, report, strict=True)

    for log_path in sorted(root.glob("bot.log*")):
        _harden_one(log_path, report, strict=True)

    data_dir = root / "data"
    if data_dir.exists() or data_dir.is_symlink():
        for path in _entries_without_following(
            data_dir,
            on_error=report.errors.append,
        ):
            _harden_one(
                path,
                report,
                strict=True,
            )
    return report


def emit_hardening_report(
    report: HardeningReport,
    *,
    emit: Callable[[str], None] | None = None,
) -> None:
    """Emit only path/permission diagnostics, never file contents."""
    writer = emit or (lambda message: print(message, file=sys.stderr))
    for message in report.skipped:
        writer("[robyx security] warning: %s" % message)
    for message in report.errors:
        writer("[robyx security] error: %s" % message)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Harden Robyx local runtime permissions",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    args = parser.parse_args(argv)
    report = harden_runtime_permissions(args.project_root)
    emit_hardening_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
