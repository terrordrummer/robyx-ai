"""Tests for spec 006 lock-file heartbeat + stale detection.

Contract: ``specs/006-continuous-task-robustness/contracts/lock-heartbeat.md``.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def sched_mod():
    import scheduler  # type: ignore
    return scheduler


# ── _write_lock_file / refresh_heartbeat ─────────────────────────────────


def test_write_lock_file_two_line_format(sched_mod, tmp_path):
    lock_file = tmp_path / "lock"
    sched_mod._write_lock_file(lock_file, 1234)
    content = lock_file.read_text()
    lines = content.strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == "1234"
    # Line 2 is ISO-8601.
    datetime.fromisoformat(lines[1])


def test_refresh_heartbeat_atomic(sched_mod, tmp_path):
    lock_file = tmp_path / "lock"
    sched_mod._write_lock_file(lock_file, 1234)
    first_ts = datetime.fromisoformat(lock_file.read_text().splitlines()[1])
    time.sleep(0.01)
    sched_mod.refresh_heartbeat(lock_file, 1234)
    second_ts = datetime.fromisoformat(lock_file.read_text().splitlines()[1])
    assert second_ts > first_ts
    # No stale temp file left behind.
    tmps = list(tmp_path.glob("*.tmp*"))
    assert tmps == []


def test_refresh_heartbeat_concurrent_writes(sched_mod, tmp_path):
    lock_file = tmp_path / "lock"
    sched_mod._write_lock_file(lock_file, 9999)

    def _writer():
        for _ in range(50):
            sched_mod.refresh_heartbeat(lock_file, 9999)

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # File is readable and still well-formed.
    content = lock_file.read_text()
    lines = content.strip().splitlines()
    assert lines[0] == "9999"
    datetime.fromisoformat(lines[1])


# ── _parse_lock_content ─────────────────────────────────────────────────


def test_parse_legacy_single_line_pid_only(sched_mod):
    pid, ts = sched_mod._parse_lock_content("42")
    assert pid == 42
    assert ts is None


def test_parse_legacy_single_line_with_ts(sched_mod):
    pid, ts = sched_mod._parse_lock_content("42 2026-04-22T12:00:00+00:00")
    assert pid == 42
    assert ts is not None
    assert ts.tzinfo is not None


def test_parse_spec_006_two_line(sched_mod):
    pid, ts = sched_mod._parse_lock_content("42\n2026-04-22T12:00:00+00:00\n")
    assert pid == 42
    assert ts is not None


def test_parse_corrupted(sched_mod):
    pid, ts = sched_mod._parse_lock_content("garbage")
    assert pid is None
    assert ts is None


def test_parse_naive_timestamp_gets_utc_tz(sched_mod):
    pid, ts = sched_mod._parse_lock_content("42\n2026-04-22T12:00:00\n")
    assert pid == 42
    assert ts is not None
    assert ts.tzinfo is timezone.utc


# ── check_lock_status ────────────────────────────────────────────────────


async def test_check_lock_status_missing(sched_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    status, pid = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.MISSING
    assert pid is None


async def test_check_lock_status_alive_with_fresh_heartbeat(
    sched_mod, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr("process.is_pid_alive", lambda pid: True)

    (tmp_path / "task-x").mkdir()
    sched_mod._write_lock_file(tmp_path / "task-x" / "lock", os.getpid())
    status, pid = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.ALIVE
    assert pid == os.getpid()


async def test_check_lock_status_stale_zombie_when_heartbeat_old(
    sched_mod, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr("process.is_pid_alive", lambda pid: True)

    import config as cfg
    monkeypatch.setattr(cfg, "LOCK_STALE_THRESHOLD_SECONDS", 1)

    (tmp_path / "task-x").mkdir()
    lock = tmp_path / "task-x" / "lock"
    # Manually write a lock with an old heartbeat.
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    lock.write_text("%d\n%s\n" % (os.getpid(), old_ts))

    status, pid = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.STALE_ZOMBIE
    assert pid == os.getpid()


async def test_check_lock_status_stale_dead_pid(
    sched_mod, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr("process.is_pid_alive", lambda pid: False)

    (tmp_path / "task-x").mkdir()
    lock = tmp_path / "task-x" / "lock"
    sched_mod._write_lock_file(lock, 99999)

    status, pid = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.STALE_DEAD_PID
    assert pid == 99999


async def test_check_lock_status_legacy_pid_only_alive_when_pid_lives(
    sched_mod, monkeypatch, tmp_path,
):
    """Pre-v0.26 locks with only a pid line must stay ALIVE while the pid runs."""
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr("process.is_pid_alive", lambda pid: True)

    (tmp_path / "task-x").mkdir()
    (tmp_path / "task-x" / "lock").write_text("%d" % os.getpid())

    status, pid = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.ALIVE
    assert pid == os.getpid()


async def test_check_lock_status_corrupt_lock_removed(
    sched_mod, monkeypatch, tmp_path,
):
    monkeypatch.setattr(sched_mod, "DATA_DIR", tmp_path)

    (tmp_path / "task-x").mkdir()
    lock = tmp_path / "task-x" / "lock"
    lock.write_text("not a valid lock")

    status, _ = await sched_mod.check_lock_status("task-x")
    assert status == sched_mod.LockStatus.MISSING
    assert not lock.exists()
