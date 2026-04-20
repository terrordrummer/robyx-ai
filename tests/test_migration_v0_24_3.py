"""Tests for bot/migrations/v0_24_3.py — continuous-task history normalisation.

The migration renames ``summary`` → ``description`` in every history
entry of every ``data/continuous/<name>/state.json``. It must be
idempotent, tolerant of unreadable files, and never touch entries that
already carry ``description``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from migrations.base import MigrationContext
import migrations.v0_24_3 as mig


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "continuous").mkdir(parents=True)
    return d


def _write_state(data_dir: Path, name: str, state: dict) -> Path:
    task_dir = data_dir / "continuous" / name
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def _read_state(data_dir: Path, name: str) -> dict:
    return json.loads(
        (data_dir / "continuous" / name / "state.json").read_text(encoding="utf-8")
    )


def _ctx(data_dir: Path) -> MigrationContext:
    return MigrationContext(data_dir=data_dir)


class TestUpgrade:
    def test_renames_summary_to_description(self, data_dir):
        _write_state(data_dir, "align-research", {
            "name": "align-research",
            "history": [
                {"step": 1, "summary": "First attempt", "artifact": "commit aaa"},
                {"step": 2, "summary": "Second attempt", "artifact": "commit bbb"},
            ],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))

        st = _read_state(data_dir, "align-research")
        assert st["history"][0]["description"] == "First attempt"
        assert "summary" not in st["history"][0]
        assert st["history"][1]["description"] == "Second attempt"
        assert "summary" not in st["history"][1]

    def test_preserves_existing_description(self, data_dir):
        _write_state(data_dir, "task-a", {
            "history": [
                # Both keys present — keep description as-is.
                {"step": 1, "description": "Real desc",
                 "summary": "Should be ignored", "artifact": "x"},
            ],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))

        st = _read_state(data_dir, "task-a")
        assert st["history"][0]["description"] == "Real desc"

    def test_leaves_entries_without_either_key_untouched(self, data_dir):
        _write_state(data_dir, "task-b", {
            "history": [
                {"step": 1, "artifact": "commit only"},
            ],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))

        st = _read_state(data_dir, "task-b")
        assert "description" not in st["history"][0]
        assert "summary" not in st["history"][0]
        assert st["history"][0]["artifact"] == "commit only"

    def test_idempotent_second_run_is_noop(self, data_dir):
        _write_state(data_dir, "task-c", {
            "history": [
                {"step": 1, "summary": "X", "artifact": "a"},
            ],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))
        first = _read_state(data_dir, "task-c")

        asyncio.run(mig.upgrade(_ctx(data_dir)))
        second = _read_state(data_dir, "task-c")

        assert first == second
        assert second["history"][0]["description"] == "X"

    def test_handles_missing_continuous_dir_gracefully(self, tmp_path):
        # No `continuous/` directory at all.
        ctx = MigrationContext(data_dir=tmp_path)
        # Must not raise.
        asyncio.run(mig.upgrade(ctx))

    def test_skips_unreadable_state_files(self, data_dir, caplog):
        task_dir = data_dir / "continuous" / "broken"
        task_dir.mkdir()
        (task_dir / "state.json").write_text("{ not valid json", encoding="utf-8")

        # Still touches the valid sibling.
        _write_state(data_dir, "healthy", {
            "history": [{"step": 1, "summary": "ok"}],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))

        healthy = _read_state(data_dir, "healthy")
        assert healthy["history"][0]["description"] == "ok"

    def test_skips_non_dict_history_entries(self, data_dir):
        _write_state(data_dir, "task-d", {
            "history": [
                "not a dict",
                {"step": 1, "summary": "mid", "artifact": "c"},
                42,
            ],
        })

        asyncio.run(mig.upgrade(_ctx(data_dir)))

        st = _read_state(data_dir, "task-d")
        # Non-dict entries preserved as-is.
        assert st["history"][0] == "not a dict"
        assert st["history"][2] == 42
        # Dict entry renamed.
        assert st["history"][1]["description"] == "mid"

    def test_empty_history_is_noop(self, data_dir):
        _write_state(data_dir, "task-e", {"history": []})
        asyncio.run(mig.upgrade(_ctx(data_dir)))
        st = _read_state(data_dir, "task-e")
        assert st["history"] == []

    def test_missing_history_key_is_noop(self, data_dir):
        _write_state(data_dir, "task-f", {"name": "task-f"})
        asyncio.run(mig.upgrade(_ctx(data_dir)))
        st = _read_state(data_dir, "task-f")
        assert "history" not in st


class TestMigrationMetadata:
    def test_chain_metadata(self):
        assert mig.MIGRATION.from_version == "0.24.2"
        assert mig.MIGRATION.to_version == "0.24.3"
        assert "description" in mig.MIGRATION.description.lower()
