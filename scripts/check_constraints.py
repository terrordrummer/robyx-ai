#!/usr/bin/env python3
"""Rebuild or verify Robyx's cross-platform dependency locks.

Runtime locks contain only production dependencies. Dev locks add tests and
quality tools. ``--universal`` retains platform markers and hashes for Linux,
macOS, and Windows instead of committing a developer-machine ``pip freeze``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


UV_VERSION = "0.11.7"
EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
PYTHON_TARGETS = ("3.10", "3.11", "3.12", "3.13", "3.14")
LOCK_SOURCES = {
    "runtime": ("bot/requirements.txt",),
    "dev": (
        "bot/requirements.txt",
        "tests/requirements-test.txt",
        "requirements/quality.in",
    ),
}


def _output_path(repo_root: Path, kind: str, version: str) -> Path:
    return (
        repo_root
        / "requirements"
        / "locks"
        / ("%s-py%s.txt" % (kind, version.replace(".", "")))
    )


def _compile(repo_root: Path, kind: str, version: str, output: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError(
            "uv is required; install the pinned tool with "
            "`python -m pip install uv==%s`" % UV_VERSION
        )

    command = [
        uv,
        "pip",
        "compile",
        "--quiet",
        *LOCK_SOURCES[kind],
        "--universal",
        "--python-version",
        version,
        "--generate-hashes",
        "--exclude-newer",
        EXCLUDE_NEWER,
        "--custom-compile-command",
        "python scripts/check_constraints.py --write",
        "--output-file",
        str(output),
    ]
    subprocess.run(command, cwd=repo_root, check=True)


def _verify_tool_version() -> None:
    result = subprocess.run(
        ["uv", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.match(r"^uv\s+(\S+)", result.stdout.strip())
    actual = match.group(1) if match else "unparseable"
    if actual != UV_VERSION:
        raise RuntimeError(
            "constraint generation requires uv %s (found %s)" % (UV_VERSION, actual)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if committed files drift")
    mode.add_argument("--write", action="store_true", help="regenerate committed files")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    _verify_tool_version()

    if args.write:
        for kind, _sources in LOCK_SOURCES.items():
            for version in PYTHON_TARGETS:
                output = _output_path(repo_root, kind, version)
                output.parent.mkdir(parents=True, exist_ok=True)
                _compile(repo_root, kind, version, output)
        return 0

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="robyx-constraints-") as temp_dir:
        for kind, _sources in LOCK_SOURCES.items():
            for version in PYTHON_TARGETS:
                expected = _output_path(repo_root, kind, version)
                candidate = Path(temp_dir) / expected.name
                _compile(repo_root, kind, version, candidate)
                if not expected.exists():
                    failures.append("missing %s" % expected.relative_to(repo_root))
                elif expected.read_bytes() != candidate.read_bytes():
                    failures.append("drift in %s" % expected.relative_to(repo_root))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        print(
            "Run `python scripts/check_constraints.py --write` with uv %s and review the diff."
            % UV_VERSION,
            file=sys.stderr,
        )
        return 1
    print("Runtime/dev locks are reproducible for Python 3.10 through 3.14.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
