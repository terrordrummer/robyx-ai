import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

import orphan_tracker


IDENTITY = {
    "start_fingerprint": "boot:100",
    "executable": "/usr/bin/codex",
    "comm": "codex",
    "pgid": 303,
}


def test_registry_round_trip_and_invalid_pid_guards(tmp_path, monkeypatch):
    registry = tmp_path / "active-pids.json"
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    orphan_tracker.register(0, owner="ignored")
    assert not registry.exists()

    identity = dict(IDENTITY, pgid=123)
    monkeypatch.setattr("process.get_process_identity_sync", lambda _pid: identity)
    orphan_tracker.register(123, owner="worker")
    assert orphan_tracker._load() == {
        "123": {"owner": "worker", "identity": identity},
    }

    orphan_tracker.unregister(-1)
    orphan_tracker.unregister(123)
    assert orphan_tracker._load() == {}


def test_atomic_save_uses_unique_temporary_files(tmp_path, monkeypatch):
    registry = tmp_path / "active-pids.json"
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    payloads = [
        {str(pid): {"owner": "worker", "identity": dict(IDENTITY, pgid=pid)}}
        for pid in range(100, 120)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(orphan_tracker._save, payloads))

    assert orphan_tracker._load() in payloads
    assert list(tmp_path.glob("active-pids.json.*.tmp")) == []


def test_invalid_registry_is_quarantined_and_blocks_mutation(tmp_path, monkeypatch):
    from persistence_recovery import PersistenceUnavailableError

    registry = tmp_path / "active-pids.json"
    registry.write_text("not-json")
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    with pytest.raises(PersistenceUnavailableError):
        orphan_tracker.register(456, owner="must-not-overwrite")
    assert not registry.exists()
    assert list(tmp_path.glob("active-pids.json.corrupt-*"))
    assert (tmp_path / "active-pids.json.recovery-pending").exists()


def test_corrupt_registry_is_startup_fatal(tmp_path, monkeypatch):
    registry = tmp_path / "active-pids.json"
    registry.write_text("not-json")
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    with pytest.raises(SystemExit, match="registry unavailable"):
        orphan_tracker.cleanup_on_startup()


def test_semantically_invalid_registry_is_rejected(tmp_path, monkeypatch):
    from persistence_recovery import PersistenceUnavailableError

    registry = tmp_path / "active-pids.json"
    registry.write_text('{"303": ["not", "metadata"]}')
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    with pytest.raises(PersistenceUnavailableError):
        orphan_tracker._load()


def test_failed_kill_preserves_live_pid_evidence(tmp_path, monkeypatch):
    registry = tmp_path / "active-pids.json"
    registry.write_text(json.dumps({"303": {"owner": "agent", "identity": IDENTITY}}))
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)
    monkeypatch.setattr("process.is_pid_alive", lambda _pid: True)
    monkeypatch.setattr("process.get_process_identity_sync", lambda _pid: IDENTITY)
    monkeypatch.setattr(orphan_tracker.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(orphan_tracker.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert orphan_tracker.cleanup_on_startup() == []
    assert orphan_tracker._load() == {
        "303": {"owner": "agent", "identity": IDENTITY},
    }


def test_cleanup_drops_dead_invalid_and_killed_entries_but_keeps_recycled_pid(
    tmp_path, monkeypatch,
):
    registry = tmp_path / "active-pids.json"
    registry.write_text(
        json.dumps({
            "bad": {},
            "101": {"owner": "dead"},
            "202": {"owner": "foreign", "identity": dict(IDENTITY, pgid=202)},
            "303": {"owner": "agent", "identity": IDENTITY},
        })
    )
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)

    alive = {202: True, 303: True}
    monkeypatch.setattr(
        "process.is_pid_alive",
        lambda pid: alive.get(pid, False),
    )
    current = {
        202: dict(IDENTITY, start_fingerprint="boot:reused", pgid=202),
        303: IDENTITY,
    }
    monkeypatch.setattr("process.get_process_identity_sync", current.get)
    monkeypatch.setattr(orphan_tracker.os, "getpgid", lambda pid: pid)

    def kill_group(pgid, _signal):
        alive[pgid] = False

    monkeypatch.setattr(orphan_tracker.os, "killpg", kill_group)
    monkeypatch.setattr(orphan_tracker, "log", MagicMock())

    assert orphan_tracker.cleanup_on_startup() == [303]
    assert orphan_tracker._load() == {
        "bad": {},
        "202": {"owner": "foreign", "identity": dict(IDENTITY, pgid=202)},
    }


def test_cleanup_preserves_unverifiable_legacy_pid_without_signalling(tmp_path, monkeypatch):
    registry = tmp_path / "active-pids.json"
    registry.write_text('{"303": {"owner": "legacy-agent"}}')
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)
    alive = {303: True}
    monkeypatch.setattr("process.is_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr("process.get_process_identity_sync", lambda _pid: IDENTITY)
    killpg = MagicMock()
    monkeypatch.setattr(orphan_tracker.os, "killpg", killpg)

    def kill_one(pid, _signal):
        alive[pid] = False

    monkeypatch.setattr(orphan_tracker.os, "kill", kill_one)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert orphan_tracker.cleanup_on_startup() == []
    killpg.assert_not_called()
    assert orphan_tracker._load() == {"303": {"owner": "legacy-agent"}}


def test_cleanup_same_pid_and_name_but_different_start_is_never_killed(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "active-pids.json"
    registry.write_text(json.dumps({
        "303": {"owner": "agent", "identity": IDENTITY},
    }))
    monkeypatch.setattr(orphan_tracker, "_PID_FILE", registry)
    monkeypatch.setattr("process.is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "process.get_process_identity_sync",
        lambda _pid: dict(IDENTITY, start_fingerprint="boot:999"),
    )
    killpg = MagicMock()
    monkeypatch.setattr(orphan_tracker.os, "killpg", killpg)

    assert orphan_tracker.cleanup_on_startup() == []
    killpg.assert_not_called()
    assert orphan_tracker._load()["303"]["identity"] == IDENTITY
