"""Resolve Robyx dependency locks for the running Python minor.

This module is stdlib-only because installers and ``_bootstrap`` must be able
to use it before any third-party package is installed.  Every declared Python
minor has an explicit lock; a missing lock is an error, never an implicit
fallback to the open-ended input requirements.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Sequence


SUPPORTED_PYTHON_MINORS = (
    (3, 10),
    (3, 11),
    (3, 12),
    (3, 13),
    (3, 14),
)
LOCK_KINDS = ("runtime", "dev")


class DependencyLockError(RuntimeError):
    """The interpreter has no usable committed dependency lock."""


def _minor(version_info: Sequence[int] | None = None) -> tuple[int, int]:
    info = version_info if version_info is not None else sys.version_info
    return int(info[0]), int(info[1])


def dependency_lock_path(
    project_root: Path,
    *,
    kind: str = "runtime",
    version_info: Sequence[int] | None = None,
    require_exists: bool = True,
) -> Path:
    """Return the exact lock for ``kind`` and an interpreter minor."""
    if kind not in LOCK_KINDS:
        raise DependencyLockError("unknown dependency lock kind: %s" % kind)
    minor = _minor(version_info)
    if minor not in SUPPORTED_PYTHON_MINORS:
        supported = ", ".join("%d.%d" % item for item in SUPPORTED_PYTHON_MINORS)
        raise DependencyLockError(
            "Python %d.%d has no Robyx dependency lock (supported: %s)"
            % (minor[0], minor[1], supported)
        )
    path = (
        Path(project_root)
        / "requirements"
        / "locks"
        / ("%s-py%d%d.txt" % (kind, minor[0], minor[1]))
    )
    if require_exists and not path.is_file():
        raise DependencyLockError("required dependency lock is missing: %s" % path)
    return path


def dependency_fingerprint(requirements_path: Path, lock_path: Path) -> str:
    """Hash both the human input and selected lock for the venv marker."""
    digest = hashlib.sha256()
    for label, path in ((b"requirements\0", requirements_path), (b"lock\0", lock_path)):
        digest.update(label)
        digest.update(Path(path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a Robyx dependency lock")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--kind", choices=LOCK_KINDS, default="runtime")
    args = parser.parse_args(argv)
    try:
        path = dependency_lock_path(args.project_root, kind=args.kind)
    except DependencyLockError as exc:
        parser.exit(2, "dependency lock error: %s\n" % exc)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
