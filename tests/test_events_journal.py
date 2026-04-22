"""Tests for bot/events.py (spec 006 event journal).

Contract: ``specs/006-continuous-task-robustness/contracts/event-journal.md``.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _iso(ts: datetime) -> str:
    return ts.isoformat()


@pytest.fixture
def events_mod(monkeypatch, tmp_path):
    """Import bot.events with EVENTS_DIR/EVENTS_HOT_FILE already patched
    by the autouse fixture. Returns the module plus a helper dict with
    the resolved paths for assertions.
    """
    import config as cfg
    import events as events_mod  # type: ignore

    # Reset module-level lock state for test isolation (state is a lock
    # object, but we want to ensure the file paths point at the tmp dir
    # via config — the fixture already patched config, so _config() picks
    # up the tmp-dir values automatically).
    hot, ev_dir, retention, max_bytes = events_mod._config()
    assert hot == cfg.EVENTS_HOT_FILE
    assert ev_dir == cfg.EVENTS_DIR
    ev_dir.mkdir(parents=True, exist_ok=True)
    hot.parent.mkdir(parents=True, exist_ok=True)
    if hot.exists():
        hot.unlink()
    return events_mod


# ── append ──────────────────────────────────────────────────────────────────


def test_append_writes_valid_jsonl_line(events_mod):
    events_mod.append(
        task_name="zeus-research",
        task_type="continuous",
        event_type="dispatched",
        outcome="ok",
        payload={"step": 12},
    )
    hot, _, _, _ = events_mod._config()
    lines = hot.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["task_name"] == "zeus-research"
    assert entry["task_type"] == "continuous"
    assert entry["event_type"] == "dispatched"
    assert entry["outcome"] == "ok"
    assert entry["payload"] == {"step": 12}
    datetime.fromisoformat(entry["ts"])  # parseable


def test_append_defaults_unknown_task_type_to_continuous(events_mod, caplog):
    events_mod.append(
        task_name="x",
        task_type="unknown-bogus",
        event_type="dispatched",
        outcome="ok",
    )
    hot, _, _, _ = events_mod._config()
    entry = json.loads(hot.read_text().splitlines()[0])
    assert entry["task_type"] == "continuous"


def test_append_concurrent_writes_do_not_interleave(events_mod):
    def _writer(n_events: int, tag: str) -> None:
        for i in range(n_events):
            events_mod.append(
                task_name=tag,
                task_type="continuous",
                event_type="dispatched",
                outcome="ok",
                payload={"step": i},
            )

    threads = [
        threading.Thread(target=_writer, args=(50, "task-a")),
        threading.Thread(target=_writer, args=(50, "task-b")),
        threading.Thread(target=_writer, args=(50, "task-c")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    hot, _, _, _ = events_mod._config()
    lines = hot.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 150
    # Every line is valid JSON (no torn writes).
    for line in lines:
        json.loads(line)


def test_append_accepts_all_valid_task_types(events_mod):
    for task_type in ("continuous", "periodic", "one-shot", "reminder"):
        events_mod.append(
            task_name="x",
            task_type=task_type,
            event_type="dispatched",
            outcome="ok",
        )
    hot, _, _, _ = events_mod._config()
    lines = hot.read_text().splitlines()
    types = [json.loads(line)["task_type"] for line in lines]
    assert types == ["continuous", "periodic", "one-shot", "reminder"]


def test_append_truncates_oversized_payload(events_mod):
    big = {"data": "x" * 4096}
    events_mod.append(
        task_name="x",
        task_type="continuous",
        event_type="step_complete",
        outcome="ok",
        payload=big,
    )
    hot, _, _, _ = events_mod._config()
    entry = json.loads(hot.read_text().splitlines()[0])
    assert entry["payload"] == {"_truncated": True}


# ── query ───────────────────────────────────────────────────────────────────


def test_query_returns_entries_in_window_newest_first(events_mod):
    now = datetime.now(timezone.utc)
    for i in range(5):
        events_mod.append(
            task_name="task-%d" % i,
            task_type="continuous",
            event_type="dispatched",
            outcome="ok",
            payload={"i": i},
        )
    # Window from 10 minutes ago — all 5 entries fall inside.
    since = now - timedelta(minutes=10)
    result = events_mod.query(since)
    assert len(result) == 5
    # Newest first (lexicographic ts ordering suffices since all entries
    # share the same day and differ only by microseconds).
    timestamps = [entry["ts"] for entry in result]
    assert timestamps == sorted(timestamps, reverse=True)


def test_query_excludes_entries_before_since(events_mod):
    events_mod.append(
        task_name="x", task_type="continuous",
        event_type="dispatched", outcome="ok",
    )
    # Window starts in the future — nothing matches.
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    result = events_mod.query(future)
    assert result == []


def test_query_filters_by_task_name(events_mod):
    events_mod.append(
        task_name="alpha", task_type="continuous",
        event_type="dispatched", outcome="ok",
    )
    events_mod.append(
        task_name="beta", task_type="continuous",
        event_type="dispatched", outcome="ok",
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    result = events_mod.query(since, task_name="alpha")
    assert len(result) == 1
    assert result[0]["task_name"] == "alpha"


def test_query_filters_by_event_type(events_mod):
    events_mod.append(
        task_name="x", task_type="continuous",
        event_type="dispatched", outcome="ok",
    )
    events_mod.append(
        task_name="x", task_type="continuous",
        event_type="step_complete", outcome="ok",
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    result = events_mod.query(since, event_type="step_complete")
    assert len(result) == 1
    assert result[0]["event_type"] == "step_complete"


def test_query_limit_clamped_to_range(events_mod):
    for i in range(50):
        events_mod.append(
            task_name="x", task_type="continuous",
            event_type="dispatched", outcome="ok",
            payload={"i": i},
        )
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    # Below-range limit clamps to 1.
    assert len(events_mod.query(since, limit=0)) == 1
    # Above-range limit clamps to 1000.
    assert len(events_mod.query(since, limit=99999)) == 50


def test_query_tolerates_corrupted_line(events_mod, caplog):
    hot, _, _, _ = events_mod._config()
    events_mod.append(
        task_name="x", task_type="continuous",
        event_type="dispatched", outcome="ok",
    )
    # Append a corrupted line manually.
    with hot.open("a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
    events_mod.append(
        task_name="y", task_type="continuous",
        event_type="step_complete", outcome="ok",
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    result = events_mod.query(since)
    # Two valid entries; corrupted line skipped.
    assert len(result) == 2
    task_names = {e["task_name"] for e in result}
    assert task_names == {"x", "y"}


# ── rotate + prune ─────────────────────────────────────────────────────────


def test_rotate_by_hour_change(events_mod, monkeypatch):
    # Inject an entry whose ts is in a previous hour.
    hot, ev_dir, _, _ = events_mod._config()
    prev_hour = datetime.now(timezone.utc) - timedelta(hours=3)
    entry = {
        "ts": _iso(prev_hour),
        "task_name": "x",
        "task_type": "continuous",
        "event_type": "dispatched",
        "outcome": "ok",
        "payload": {},
    }
    hot.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    rotated = events_mod.rotate_if_needed()
    assert rotated is not None
    assert rotated.parent == ev_dir
    # Shard exists and contains the entry.
    assert rotated.exists()
    # Hot file is now empty or freshly touched.
    assert hot.exists()
    assert hot.read_text() == ""


def test_rotate_by_size_threshold(events_mod, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "EVENT_MAX_HOT_BYTES", 100)  # tiny threshold

    for i in range(10):
        events_mod.append(
            task_name="x",
            task_type="continuous",
            event_type="dispatched",
            outcome="ok",
            payload={"i": i, "pad": "x" * 50},
        )
    rotated = events_mod.rotate_if_needed()
    assert rotated is not None


def test_rotate_no_op_when_hot_is_empty(events_mod):
    assert events_mod.rotate_if_needed() is None


def test_prune_retention_removes_old_shards(events_mod):
    _, ev_dir, _, _ = events_mod._config()
    ev_dir.mkdir(parents=True, exist_ok=True)

    # Create an old shard (10 days ago) and a recent one.
    old_start = datetime.now(timezone.utc) - timedelta(days=10)
    old_name = "events-%s.jsonl" % old_start.strftime("%Y%m%d-%H")
    (ev_dir / old_name).write_text("", encoding="utf-8")

    recent_start = datetime.now(timezone.utc) - timedelta(hours=2)
    recent_name = "events-%s.jsonl" % recent_start.strftime("%Y%m%d-%H")
    (ev_dir / recent_name).write_text("", encoding="utf-8")

    removed = events_mod.prune_retention(max_age_days=7)
    assert removed == 1
    assert not (ev_dir / old_name).exists()
    assert (ev_dir / recent_name).exists()


def test_query_reads_across_shard_boundary(events_mod):
    # Write to hot, rotate, append again — query must see both.
    events_mod.append(
        task_name="before-rotate",
        task_type="continuous",
        event_type="dispatched",
        outcome="ok",
    )
    # Force rotation by editing the hot file's first-line ts to a previous hour.
    hot, _, _, _ = events_mod._config()
    lines = hot.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["ts"] = _iso(datetime.now(timezone.utc) - timedelta(hours=3))
    hot.write_text(json.dumps(entry) + "\n")
    events_mod.rotate_if_needed()

    events_mod.append(
        task_name="after-rotate",
        task_type="continuous",
        event_type="dispatched",
        outcome="ok",
    )

    since = datetime.now(timezone.utc) - timedelta(hours=5)
    result = events_mod.query(since)
    names = {e["task_name"] for e in result}
    assert names == {"before-rotate", "after-rotate"}
