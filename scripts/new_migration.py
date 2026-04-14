#!/usr/bin/env python3
"""Scaffold a new versioned migration module.

Usage:
    python scripts/new_migration.py 0.20.13 [--from 0.20.12] [--description "..."]

Creates ``bot/migrations/v0_20_13.py`` with a no-op upgrade and the
correct ``from_version`` / ``to_version`` fields so the chain stays
continuous. If ``--from`` is omitted, the previous release version is
inferred from the ``releases/`` directory.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TEMPLATE = '''\
"""{from_ver} → {to_ver} — {description}.

{body}
"""

from __future__ import annotations

from .base import Migration, MigrationContext


async def upgrade(ctx: MigrationContext) -> None:
    # TODO: implement migration logic here, or leave as no-op if this
    # release ships no user-visible data / state changes.
    return None


MIGRATION = Migration(
    from_version="{from_ver}",
    to_version="{to_ver}",
    description="{description}",
    upgrade=upgrade,
)
'''


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _previous_version(target: str, repo_root: Path) -> str:
    pat = re.compile(r"^(\d+\.\d+\.\d+)\.md$")
    versions: list[str] = []
    for f in (repo_root / "releases").iterdir():
        m = pat.match(f.name)
        if m:
            versions.append(m.group(1))
    target_t = _version_tuple(target)
    earlier = [v for v in versions if _version_tuple(v) < target_t]
    if not earlier:
        raise SystemExit(
            "Cannot infer previous version: no release older than %s "
            "found in releases/. Pass --from explicitly." % target
        )
    earlier.sort(key=_version_tuple)
    return earlier[-1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("version", help="Target version, e.g. 0.20.13")
    p.add_argument(
        "--from", dest="from_version",
        help="Previous version (auto-detected from releases/ if omitted)",
    )
    p.add_argument(
        "--description", default="",
        help="Short human-readable description for the migration docstring",
    )
    args = p.parse_args(argv)

    target = args.version.strip()
    if not re.match(r"^\d+\.\d+\.\d+$", target):
        p.error("version must look like X.Y.Z, got %r" % target)

    repo_root = Path(__file__).resolve().parents[1]
    from_ver = args.from_version or _previous_version(target, repo_root)

    slug = "v" + target.replace(".", "_") + ".py"
    out_path = repo_root / "bot" / "migrations" / slug
    if out_path.exists():
        p.error("migration already exists: %s" % out_path)

    description = args.description or "no-op release bump"
    body = (
        "This release has no user-visible data changes; the migration "
        "exists purely to keep the version chain continuous."
        if not args.description
        else "Fill in the rationale and the specific state this step mutates."
    )

    out_path.write_text(TEMPLATE.format(
        from_ver=from_ver, to_ver=target,
        description=description, body=body,
    ))
    print("Created %s (%s → %s)" % (out_path.relative_to(repo_root), from_ver, target))
    print(
        "The runner will auto-discover this module via pkgutil.iter_modules — "
        "no explicit import needed in __init__.py."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
