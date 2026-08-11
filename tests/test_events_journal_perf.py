"""Performance sanity gates for the spec-006 append-only event journal."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone


def test_event_journal_contract_latency(monkeypatch, tmp_path):
    import config
    import events

    hot = tmp_path / "events.jsonl"
    shards = tmp_path / "events"
    monkeypatch.setattr(config, "EVENTS_HOT_FILE", hot)
    monkeypatch.setattr(config, "EVENTS_DIR", shards)
    monkeypatch.setattr(config, "EVENT_MAX_HOT_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(config, "EVENT_RETENTION_DAYS", 7)

    append_latencies_ms: list[float] = []
    for index in range(10_000):
        started = time.perf_counter()
        events.append(
            task_name="perf-%d" % (index % 10),
            task_type="continuous",
            event_type="step_complete",
            outcome="ok",
            payload={"step": index},
        )
        append_latencies_ms.append((time.perf_counter() - started) * 1000)

    p95_append = statistics.quantiles(append_latencies_ms, n=100)[94]
    assert p95_append <= 50

    started = time.perf_counter()
    result = events.query(
        datetime.now(timezone.utc) - timedelta(hours=24),
        limit=1000,
    )
    query_ms = (time.perf_counter() - started) * 1000
    assert len(result) == 1000
    assert query_ms <= 500

    monkeypatch.setattr(config, "EVENT_MAX_HOT_BYTES", 1)
    started = time.perf_counter()
    rotated = events.rotate_if_needed()
    rotation_ms = (time.perf_counter() - started) * 1000
    assert rotated is not None
    assert rotation_ms <= 100
