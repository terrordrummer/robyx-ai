"""Regression coverage for retiring the legacy periodic system monitor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrations import v0_29_3
from migrations.base import MigrationContext
from persistence_recovery import PersistenceUnavailableError


def _ctx(data_dir: Path) -> MigrationContext:
    return MigrationContext(data_dir=data_dir)


def _periodic(name: str, agent_file: str) -> dict:
    return {
        "id": "periodic-%s" % name,
        "name": name,
        "agent_file": agent_file,
        "type": "periodic",
        "status": "pending",
        "interval_seconds": 21600,
        "next_run": "2026-08-13T12:00:00+00:00",
    }


async def test_removes_only_legacy_periodic_monitor_entries(tmp_path):
    queue_path = tmp_path / "queue.json"
    entries = [
        _periodic("system-monitor", "agents/system-monitor.md"),
        _periodic("old-alias", "agents\\system-monitor.md"),
        _periodic("weekly-summary", "agents/weekly-summary.md"),
        {
            "id": "diagnostic-once",
            "name": "system-monitor",
            "agent_file": "agents/system-monitor.md",
            "type": "one-shot",
            "status": "pending",
        },
        {
            "id": "reminder-1",
            "message": "Review deployment",
            "fire_at": "2026-08-13T12:00:00+00:00",
            "type": "reminder",
            "status": "pending",
        },
    ]
    queue_path.write_text(json.dumps(entries), encoding="utf-8")

    await v0_29_3.upgrade(_ctx(tmp_path))

    migrated = json.loads(queue_path.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in migrated] == [
        "periodic-weekly-summary",
        "diagnostic-once",
        "reminder-1",
    ]


async def test_is_idempotent_and_does_not_rewrite_clean_queue(tmp_path):
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(
        json.dumps([_periodic("weekly-summary", "agents/weekly-summary.md")]),
        encoding="utf-8",
    )
    before = queue_path.read_bytes()

    await v0_29_3.upgrade(_ctx(tmp_path))
    await v0_29_3.upgrade(_ctx(tmp_path))

    assert queue_path.read_bytes() == before
    assert not queue_path.with_suffix(".json.v0_29_3.tmp").exists()


async def test_missing_queue_is_not_created(tmp_path):
    await v0_29_3.upgrade(_ctx(tmp_path))
    assert not (tmp_path / "queue.json").exists()


async def test_corrupt_queue_fails_closed_without_clean_overwrite(tmp_path):
    queue_path = tmp_path / "queue.json"
    queue_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(PersistenceUnavailableError):
        await v0_29_3.upgrade(_ctx(tmp_path))

    assert not queue_path.exists()
    assert list(tmp_path.glob("queue.json.corrupt-*"))


def test_migration_metadata_keeps_version_chain_continuous():
    assert v0_29_3.MIGRATION.from_version == "0.29.2"
    assert v0_29_3.MIGRATION.to_version == "0.29.3"
    assert v0_29_3.MIGRATION.upgrade is v0_29_3.upgrade
