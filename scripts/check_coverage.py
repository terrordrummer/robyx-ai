#!/usr/bin/env python3
"""Enforce coverage floors for Robyx's highest-risk runtime boundaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Floors are ratchets, not the desired end-state. The improvement targets and
# the rationale for each boundary are tracked in docs/quality.md.
FILE_FLOORS = {
    # Legacy floors never sit below the August review baseline.
    "bot/bot.py": 28.0,
    "bot/scheduler.py": 68.0,
    "bot/orphan_tracker.py": 46.0,
    "bot/messaging/telegram.py": 53.0,
    # New security/resilience boundaries start at 85%, not as legacy debt.
    "bot/execution_policy.py": 100.0,
    "bot/persistence_recovery.py": 85.0,
    "bot/runtime_supervisor.py": 85.0,
    "bot/topic_recovery.py": 85.0,
    "bot/local_security.py": 85.0,
    "bot/config_schema.py": 85.0,
    "bot/dependency_locks.py": 85.0,
    "bot/config_command_service.py": 85.0,
    "bot/maintenance.py": 85.0,
    "bot/task_scope.py": 85.0,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path)
    args = parser.parse_args(argv)

    report = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    files = report.get("files", {})
    failures: list[str] = []
    for filename, floor in FILE_FLOORS.items():
        try:
            percent = float(files[filename]["summary"]["percent_covered"])
        except (KeyError, TypeError, ValueError):
            failures.append("%s is missing from the coverage report" % filename)
            continue
        if percent + 1e-9 < floor:
            failures.append("%s %.2f%% is below %.2f%%" % (filename, percent, floor))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("Risk-path coverage floors satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
