# Contract — Event Journal (`bot/events.py`)

## Files on disk

- Hot file: `data/events.jsonl`
- Rotated shards: `data/events/events-YYYYMMDD-HH.jsonl`
- Retention: shards older than 7 days are deleted on each rotation cycle (configurable `EVENT_RETENTION_DAYS`).

## Entry format (one JSONL line per event)

```json
{"ts":"2026-04-22T14:30:15.123456+00:00","task_name":"zeus-research","task_type":"continuous","event_type":"step_complete","outcome":"awaiting_input","payload":{"step":12,"duration_s":1243,"question":"multi-ref or calibration?"}}
```

### Field contract

| Field | Type | Required | Notes |
|---|---|---|---|
| `ts` | string (ISO-8601 UTC with microseconds and `+00:00` suffix) | YES | Generated via `datetime.now(timezone.utc).isoformat()` |
| `task_name` | string | YES | Task safe_name; may be empty string `""` for bot-level events (future-use) |
| `task_type` | string | YES | One of `continuous | periodic | one-shot | reminder`. REQUIRED from day one to avoid migration for Q5 |
| `event_type` | string | YES | See § Event-type taxonomy |
| `outcome` | string | YES | Short label; free-form but SHOULD be one of `success | reverted | failed | awaiting_input | completed | stopped | deleted | recovered | timeout | ...` |
| `payload` | object | no | ≤ 1 KB serialised; truncated with `{"_truncated": true}` marker if larger |

### Event-type taxonomy

**Continuous (MVP scope):**
- `created` — task row first written
- `dispatched` — scheduler spawned a step subprocess
- `step_start` — subprocess wrote `step_start` heartbeat (new)
- `step_complete` — subprocess exited with delivery posted
- `state_transition` — any status change
- `stopped`, `resumed`, `completed`, `deleted`, `archived`
- `error` — terminal error state entered
- `orphan_incident` — 3-cycle backoff triggered (escalation)
- `orphan_recovery` — task self-recovered after an orphan detection without hitting the incident threshold
- `rate_limited`, `rate_limit_recovered`
- `lock_heartbeat_stale` — scheduler observed stale heartbeat
- `lock_recovered` — stale lock reclaimed by scheduler
- `drain_started`, `drain_completed`, `drain_timeout`
- `hq_fallback_sent` — FR-002a last-resort surface used
- `migration` — seeded by `v0_26_0.py`

**Reserved (no-op hooks in MVP; not emitted by this feature):**
- `periodic_fired`, `periodic_completed`
- `oneshot_fired`, `oneshot_completed`
- `reminder_fired`, `reminder_delivered`, `reminder_failed`

## Public Python API

```python
# bot/events.py

def append(
    task_name: str,
    task_type: Literal["continuous", "periodic", "one-shot", "reminder"],
    event_type: str,
    outcome: str,
    payload: dict | None = None,
) -> None
    """Append a single event to the current hot journal.

    Thread-safe (uses process-wide lock). Atomic per-line on POSIX for
    entries ≤ PIPE_BUF bytes; entries that would exceed are truncated
    with the `_truncated` marker in payload.
    """


def query(
    since: datetime,
    task_name: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
) -> list[dict]
    """Return entries within [since, now], newest first (reverse chronological),
    capped at `limit`. Scans the hot file plus any rotated shards intersecting
    the window. Filters applied in-memory after file read.

    `limit` is clamped to [1, 1000].
    """


def rotate_if_needed() -> Path | None
    """Called by scheduler once per cycle. Rotates if:
      - current hour differs from hot-file hour, OR
      - hot file exceeds EVENT_MAX_HOT_BYTES (10 MB default)
    Also prunes shards older than EVENT_RETENTION_DAYS (7 default).
    Returns the rotated shard path, or None if no rotation.
    """


def prune_retention(max_age_days: int = 7) -> int
    """Remove rotated shards older than max_age_days. Returns count removed."""
```

## Atomicity

- Append uses a process-wide `threading.Lock` to serialise writes within a single bot process.
- Across processes: the journal is written only by the main bot process (not by step subprocesses). Subprocesses never append directly — they return outputs / status that the parent then journals. This avoids cross-process write contention and keeps POSIX atomicity guarantees sufficient.
- Rotation uses `os.rename` (atomic on same filesystem).

## Failure modes and recovery

- **Disk full on append**: log ERROR, skip the entry, do not retry; bot stays up. (Journal gaps are preferable to a bot crash; missing entries are annotated implicitly — a user asking "what happened" will see a gap but the system continues.)
- **Corrupted line on read** (from a previously crashed partial write, pre-atomicity guarantee breach): skip the line, log WARN with line number and shard, continue. Query returns the remaining entries.
- **Rotation failure mid-cycle**: the hot file is left intact; next cycle retries rotation.

## Retention and query window

- Default retention: 7 days.
- Queries requesting a window older than retention are clamped, and an `_incomplete: true` marker is attached to the result.

## Observability of the observability layer

- Metrics (via `log.info`): rotation events, prune counts, append failures, query stats (count / duration).
- Periodic sanity check (daily): compare shard count vs retention window; WARN if shards older than retention exist.

## Non-goals

- Multi-writer support across processes (by design — only the parent bot writes).
- Full-text search, aggregation, joins.
- Streaming / tailing for external consumers (consumers query on demand).
