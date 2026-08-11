"""RR-04 regression tests for fail-closed runtime JSON recovery."""

import io
import json
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import continuous as cont
import persistence_recovery as recovery
import scheduler as sched
from persistence_recovery import PersistenceUnavailableError
from task_scope import TaskScope


_SCOPE = TaskScope("telegram", "-1001", 42)


@pytest.fixture(autouse=True)
def _runtime_paths(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    continuous_dir = data_dir / "continuous"
    continuous_dir.mkdir(parents=True)
    monkeypatch.setattr(sched, "DATA_DIR", data_dir)
    monkeypatch.setattr(sched, "QUEUE_FILE", data_dir / "queue.json")
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", continuous_dir)
    monkeypatch.setattr(sched, "_startup_cleanup_done", True)
    return data_dir


def _write_snapshot(
    data_dir: Path,
    name: str,
    members: dict[str, object | bytes],
    *,
    mtime: int,
) -> Path:
    backups = data_dir / "backups"
    backups.mkdir(exist_ok=True)
    snapshot = backups / name
    with tarfile.open(snapshot, "w:gz") as archive:
        for member_name, value in members.items():
            content = value if isinstance(value, bytes) else json.dumps(value).encode()
            info = tarfile.TarInfo("./" + member_name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    os.utime(snapshot, (mtime, mtime))
    return snapshot


def _one_shot(name: str) -> dict:
    return {
        "id": "id-" + name,
        "name": name,
        "agent_file": "agents/test.md",
        "type": "one-shot",
        "scheduled_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "status": "pending",
    }


def _corrupt_queue() -> None:
    sched.QUEUE_FILE.write_text("not-json", encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda: sched.add_task(_one_shot("new"), scope=_SCOPE),
        lambda: sched.add_reminder(
            {"message": "x", "fire_at": "2099-01-01T00:00:00+00:00"},
            scope=_SCOPE,
        ),
        lambda: sched.save_queue([_one_shot("replacement")]),
        lambda: sched.cancel_task_by_name("old"),
        lambda: sched.cancel_tasks_for_agent_file("agents/test.md"),
        lambda: sched.reactivate_continuous_task_by_name("old"),
        lambda: sched._reconcile_task_results([
            {"id": "old", "claim_token": "claim", "status": "dispatched"}
        ]),
        lambda: sched._reconcile_reminder_results([
            {"id": "old", "claim_token": "claim", "status": "sent", "sent_at": "now"}
        ]),
    ],
    ids=[
        "add-task",
        "add-reminder",
        "replace-update",
        "cancel-by-name",
        "cancel-by-agent",
        "reactivate",
        "task-reconcile",
        "reminder-reconcile",
    ],
)
def test_queue_mutations_fail_closed_without_snapshot(mutation):
    _corrupt_queue()

    with pytest.raises((sched.QueueUnavailableError, PersistenceUnavailableError)):
        mutation()

    assert not sched.QUEUE_FILE.exists()
    assert recovery.recovery_marker_path(sched.QUEUE_FILE).exists()
    quarantined = list(sched.QUEUE_FILE.parent.glob("queue.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "not-json"


def test_corrupt_then_add_recovers_snapshot_before_mutation(_runtime_paths):
    _write_snapshot(
        _runtime_paths,
        "pre-update-old-to-new-20260811T000000Z.tar.gz",
        {"queue.json": [_one_shot("preserved")]},
        mtime=100,
    )
    _corrupt_queue()

    sched.add_task(_one_shot("added"), scope=_SCOPE)

    assert [entry["name"] for entry in sched.load_queue()] == ["preserved", "added"]
    assert list(_runtime_paths.glob("queue.json.corrupt-*"))
    assert not recovery.recovery_marker_path(sched.QUEUE_FILE).exists()


def test_semantically_invalid_queue_and_snapshot_fail_closed(_runtime_paths):
    _write_snapshot(
        _runtime_paths,
        "pre-update-semantic-invalid.tar.gz",
        {"queue.json": [{"id": "looks-like-json"}]},
        mtime=100,
    )
    sched.QUEUE_FILE.write_text('[{"unknown": "entry"}]', encoding="utf-8")

    with pytest.raises(sched.QueueUnavailableError):
        sched.load_queue()

    assert list(_runtime_paths.glob("queue.json.corrupt-*"))


def test_public_queue_writes_reject_unscoped_or_malformed_candidates():
    with pytest.raises(ValueError, match="workspace scope"):
        sched.add_task(_one_shot("unscoped"))
    with pytest.raises(ValueError, match="workspace scope"):
        sched.add_reminder({"message": "x", "fire_at": "2099-01-01T00:00:00+00:00"})
    with pytest.raises(ValueError, match="malformed scheduler queue"):
        sched.save_queue([{"id": "empty-shell"}])


def test_newest_bad_snapshot_falls_back_to_older_valid(_runtime_paths):
    _write_snapshot(
        _runtime_paths,
        "pre-update-a-to-b-older.tar.gz",
        {"queue.json": [_one_shot("from-older")]},
        mtime=100,
    )
    _write_snapshot(
        _runtime_paths,
        "pre-update-b-to-c-newer.tar.gz",
        {"queue.json": b'{"valid_json":"wrong_shape"}'},
        mtime=200,
    )
    _corrupt_queue()

    entries = sched.load_queue()

    assert [entry["name"] for entry in entries] == ["from-older"]


def test_interrupted_install_resumes_from_durable_marker(
    _runtime_paths,
    monkeypatch,
):
    _write_snapshot(
        _runtime_paths,
        "pre-update-a-to-b-crash.tar.gz",
        {"queue.json": [_one_shot("recovered-after-crash")]},
        mtime=100,
    )
    _corrupt_queue()
    original_atomic_write = recovery._atomic_write_bytes
    interrupted = False

    def fail_first_recovery_install(path, content, *, suffix):
        nonlocal interrupted
        if Path(path) == sched.QUEUE_FILE and suffix == ".recover-tmp" and not interrupted:
            interrupted = True
            raise OSError("simulated process interruption")
        return original_atomic_write(path, content, suffix=suffix)

    monkeypatch.setattr(recovery, "_atomic_write_bytes", fail_first_recovery_install)
    with pytest.raises(sched.QueueUnavailableError):
        sched.load_queue()

    assert not sched.QUEUE_FILE.exists()
    assert recovery.recovery_marker_path(sched.QUEUE_FILE).exists()

    entries = sched.load_queue()
    assert [entry["name"] for entry in entries] == ["recovered-after-crash"]
    assert not recovery.recovery_marker_path(sched.QUEUE_FILE).exists()


@pytest.mark.asyncio
async def test_scheduler_reports_explicit_degraded_queue_event(monkeypatch):
    _corrupt_queue()
    journal = MagicMock()
    monkeypatch.setattr(sched, "_journal_scheduler_event", journal)

    result = await sched.run_scheduler_cycle(MagicMock())

    assert result == {
        "dispatched": [],
        "errors": ["queue_unavailable"],
        "reminders_sent": 0,
        "degraded": True,
    }
    journal.assert_called_once_with(
        task_name="scheduler-queue",
        task_type="scheduler",
        event_type="queue_unavailable",
        outcome="failed_closed",
        payload={"operation": "claim"},
    )


@pytest.mark.asyncio
async def test_continuous_scheduler_observes_corrupt_state(monkeypatch):
    name = "broken-state"
    state_path = cont.state_file_path(name)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("broken", encoding="utf-8")
    sched.save_queue([{
        "id": "continuous-broken",
        "name": name,
        "type": "continuous",
        "status": "pending",
        "state_file": str(state_path),
    }])
    journal = MagicMock()
    monkeypatch.setattr(sched, "_journal_scheduler_event", journal)

    dispatched, errors = await sched._handle_continuous_entries(MagicMock())

    assert dispatched == []
    assert errors == [name]
    journal.assert_called_once_with(
        task_name=name,
        event_type="state_unavailable",
        outcome="failed_closed",
        payload={"operation": "continuous_dispatch"},
    )


def _continuous_state(name: str, *, status: str = "pending") -> dict:
    return {
        "name": name,
        "status": status,
        "history": [],
        "current_step": None,
        "next_step": {"number": 1, "description": "continue"},
    }


def test_continuous_state_recovers_verified_snapshot(_runtime_paths):
    path = cont.state_file_path("recoverable")
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    _write_snapshot(
        _runtime_paths,
        "pre-update-state-recovery.tar.gz",
        {"continuous/recoverable/state.json": _continuous_state("recoverable")},
        mtime=100,
    )

    state = cont.load_state(path)

    assert state["name"] == "recoverable"
    assert state["status"] == "pending"
    assert list(path.parent.glob("state.json.corrupt-*"))


def test_continuous_recovery_skips_semantically_invalid_newest_snapshot(
    _runtime_paths,
):
    path = cont.state_file_path("semantic-recovery")
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    _write_snapshot(
        _runtime_paths,
        "pre-update-valid-older.tar.gz",
        {
            "continuous/semantic-recovery/state.json":
                _continuous_state("semantic-recovery")
        },
        mtime=100,
    )
    _write_snapshot(
        _runtime_paths,
        "pre-update-invalid-newer.tar.gz",
        {"continuous/semantic-recovery/state.json": {"x": 1}},
        mtime=200,
    )

    recovered = cont.load_state(path)

    assert recovered["name"] == "semantic-recovery"
    assert recovered["status"] == "pending"


def test_continuous_corruption_cannot_be_recreated_without_snapshot():
    path = cont.state_file_path("unrecoverable")
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")

    with pytest.raises(PersistenceUnavailableError):
        cont.load_state(path)
    with pytest.raises(PersistenceUnavailableError):
        cont.save_state(path, _continuous_state("stale-overwrite"))

    assert not path.exists()
    assert recovery.recovery_marker_path(path).exists()
    assert len(list(path.parent.glob("state.json.corrupt-*"))) == 1


def test_write_that_discovers_corruption_recovers_but_rejects_stale_state(
    _runtime_paths,
):
    path = cont.state_file_path("write-race")
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    recovered = _continuous_state("write-race", status="stopped")
    _write_snapshot(
        _runtime_paths,
        "pre-update-state-write-race.tar.gz",
        {"continuous/write-race/state.json": recovered},
        mtime=100,
    )

    with pytest.raises(PersistenceUnavailableError, match="reload before retrying"):
        cont.save_state(path, _continuous_state("write-race", status="running"))

    assert recovery.unavailable_marker_path(path).exists()
    assert cont.load_state(path)["status"] == "stopped"
    assert not recovery.unavailable_marker_path(path).exists()


def test_migration_does_not_overwrite_interrupted_queue_recovery(
    _runtime_paths,
):
    recovery.recovery_marker_path(sched.QUEUE_FILE).write_text("{}", encoding="utf-8")

    with pytest.raises(sched.QueueUnavailableError):
        sched.migrate_to_unified_queue()

    assert not sched.QUEUE_FILE.exists()
    assert recovery.recovery_marker_path(sched.QUEUE_FILE).exists()


def test_recovery_refuses_target_outside_data_directory(tmp_path, caplog):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    result = recovery._recover_from_snapshots(
        tmp_path / "outside.json",
        data_dir=data_dir,
        validator=lambda value: isinstance(value, list),
        kind="queue",
        logger=recovery.logging.getLogger("test.recovery"),
    )

    assert result is None
    assert "outside data directory" in caplog.text


def test_recovery_without_snapshot_directory_fails_closed(tmp_path, caplog):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    result = recovery._recover_from_snapshots(
        data_dir / "queue.json",
        data_dir=data_dir,
        validator=lambda value: isinstance(value, list),
        kind="queue",
        logger=recovery.logging.getLogger("test.recovery.no-backup"),
    )

    assert result is None
    assert "no updater snapshots" in caplog.text


def test_recovery_exhausts_snapshots_without_target_member(tmp_path, caplog):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    _write_snapshot(
        data_dir,
        "pre-update-unrelated.tar.gz",
        {"other.json": []},
        mtime=100,
    )

    result = recovery._recover_from_snapshots(
        data_dir / "queue.json",
        data_dir=data_dir,
        validator=lambda value: isinstance(value, list),
        kind="queue",
        logger=recovery.logging.getLogger("test.recovery.exhausted"),
    )

    assert result is None
    assert "No usable snapshot" in caplog.text


def test_json_recovery_rejects_oversized_live_content_before_decode():
    """The recovery boundary must enforce its byte cap before JSON parsing."""
    oversized = b" " * (recovery.MAX_RECOVERY_FILE_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds recovery size limit"):
        recovery._decode_valid_json(oversized, lambda value: True)
