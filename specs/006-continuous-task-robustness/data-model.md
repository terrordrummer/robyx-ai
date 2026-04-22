# Phase 1 Data Model — Continuous-Task Observability & Lifecycle Robustness

Extends the existing Robyx persistent state. All new fields are additive; older state files are tolerated on load (defaults applied). Every write is atomic (temp-file + `os.replace`).

---

## 1. ContinuousTask (extends existing `data/continuous/<name>/state.json`)

Existing fields (from spec 005 / v0_23_0) preserved as-is. New / modified fields:

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `status` | `Literal["created", "running", "awaiting_input", "rate_limited", "stopped", "completed", "error", "deleted"]` | yes | `"created"` | Authoritative lifecycle state (R9). Underscore form canonical — legacy `"awaiting-input"` and `"rate-limited"` accepted on read, rewritten to underscore form on next save |
| `dedicated_thread_id` | `int | None` | yes after migration | `None` | Platform thread/channel id of the task's own topic. Set by `topics.create_continuous_workspace` (new create path) or `v0_26_0.py` (migration). Before migration or on platforms without topic primitives: `None` → delivery falls back to parent workspace thread with inline state markers |
| `drain_timeout_seconds` | `int` | no | `3600` | Per-task drain window on workspace-close (clarify Q4). Override at create time via `[CONTINUOUS drain_timeout="…"]` |
| `awaiting_question` | `str | None` | no | `None` | (Existing) Pending user-visible question. Cleared on resume |
| `awaiting_since_ts` | `str (ISO-8601 UTC) | None` | no | `None` | Timestamp of transition into `awaiting_input`. Cleared on resume |
| `awaiting_pinned_msg_id` | `int | None` | no | `None` | Platform message id of the pinned awaiting-input message. Cleared on unpin |
| `awaiting_reminder_sent_ts` | `str (ISO-8601 UTC) | None` | no | `None` | Timestamp of the one-and-only 24h reminder posted for the current awaiting episode. Cleared on resume |
| `orphan_detect_count` | `int` | no | `0` | Consecutive scheduler cycles that detected an orphan condition for this task. Resets to 0 on successful recovery; triggers backoff + incident at ≥ 3 |
| `orphan_last_detected_ts` | `str (ISO-8601 UTC) | None` | no | `None` | Timestamp of last orphan detection (used to confirm consecutiveness — must be within 2× scheduler cycle) |
| `hq_fallback_sent` | `bool` | no | `false` | FR-002a suppression flag. Prevents duplicate HQ last-resort messages per unreachable-topic episode |
| `topic_unreachable_since_ts` | `str (ISO-8601 UTC) | None` | no | `None` | First detected unreachability timestamp. Cleared on successful recreation. Used to bound retry window |
| `archived_at` | `str (ISO-8601 UTC) | None` | no | `None` | Set by `delete` operation once topic has been archived. Task row kept as tombstone |
| `migrated_v0_26_0` | `str (ISO-8601 UTC) | None` | no | `None` | Idempotency gate for the migration |

### State transition invariants

- `status → deleted` MUST set `archived_at` after topic archive succeeds.
- `awaiting_question` MUST be non-null whenever `status == "awaiting_input"`.
- `awaiting_pinned_msg_id` MAY be null even when `status == "awaiting_input"` (e.g. on Slack adapter where pinning is no-op or when pin failed and was retried).
- `awaiting_reminder_sent_ts` MUST be null on entry to a fresh `awaiting_input` episode.
- `orphan_detect_count` MUST be reset to 0 upon any successful step start (even for a newly dispatched step replacing a crashed one).
- Legacy hyphen-form status values (`"awaiting-input"`, `"rate-limited"`) MUST be accepted on read and rewritten to underscore form on the next `save_state()`.

### Derived fields (not persisted, recomputed)

- `is_resumable := status in {"stopped", "awaiting_input"}`
- `is_terminal  := status in {"completed", "deleted"}` (no dispatch; no resume)
- `is_blocked   := status in {"rate_limited", "error"}` (no dispatch; may auto-recover for `rate_limited`, requires user intervention for `error`)

---

## 2. EventJournalEntry (new; one JSONL line at `data/events.jsonl` or rotated shard)

| Field | Type | Required | Purpose |
|---|---|---|---|
| `ts` | `str (ISO-8601 UTC, microsecond precision)` | yes | Event occurrence time |
| `task_name` | `str` | yes | Canonical safe_name of the task |
| `task_type` | `Literal["continuous", "periodic", "one-shot", "reminder"]` | yes | For forward-compat (clarify Q5) |
| `event_type` | `str` | yes | See R2 taxonomy in research.md |
| `outcome` | `str` | yes | Short human label (`"success"`, `"reverted"`, `"awaiting_input"`, etc.) |
| `payload` | `dict` | no | Structured metadata ≤ 1 KB (`{"truncated": true}` flag if capped) |

### Invariants

- `ts` monotonically increases within a single process (use `datetime.now(timezone.utc)` per append; logical ordering is file order).
- No partial lines on disk: each append is a single `write()` call with newline appended to the JSON-serialised object; enforced by buffered I/O sized to the expected per-entry budget.
- Consumers tolerate entries with unknown `event_type` values (forward-compat).
- `payload` serialisation MUST NOT contain raw newlines (serialised via `json.dumps(..., separators=(",", ":"))`).

### Query contract (interface into `bot/events.py`)

```python
def append(task_name: str, task_type: str, event_type: str, outcome: str, payload: dict | None = None) -> None
def query(since: datetime, task_name: str | None = None, event_type: str | None = None, limit: int = 200) -> list[EventJournalEntry]
def rotate_if_needed() -> Path | None  # called by scheduler per cycle
def prune_retention(max_age_days: int = 7) -> int  # returns count of shards removed
```

---

## 3. LockFile (extends existing `data/<task_name>/lock` or `data/continuous/<task_name>/lock`)

| Line | Content | Notes |
|---|---|---|
| 1 | `<pid>` | As today |
| 2 | `<iso8601_heartbeat_ts>` | NEW — refreshed every 30 s by running subprocess; absent → legacy-format lock, treated as stale at next cycle |

### Invariants

- Writes are atomic (`temp-file + os.replace`) — scheduler-side cleanup MUST accept transient "missing second line" as "refresh in progress, not stale".
- `check_lock()` returns one of `{"alive", "stale_dead_pid", "stale_old_heartbeat", "missing"}`:
  - `alive`: pid running AND (heartbeat missing OR heartbeat ≤ 5 min old — legacy-format locks remain alive until pid death)
  - `stale_dead_pid`: pid no longer exists on the OS → safe to remove
  - `stale_old_heartbeat`: pid exists but heartbeat > 5 min old → zombie; caller SHOULD attempt targeted termination before removing lock
  - `missing`: no lock file

---

## 4. DedicatedTopic (platform-side; not persisted directly — referenced by `dedicated_thread_id`)

| Property | Source | Purpose |
|---|---|---|
| `thread_id` | `dedicated_thread_id` on task state | Platform channel/topic/thread id |
| `title` | Platform state | Updated via `edit_topic_title`; carries state marker (e.g. `[Continuous] zeus-research · ⏸`) |
| `pinned_message_id` | `awaiting_pinned_msg_id` on task state | One at a time; cleared on resume |
| `archived` | Platform state | Set on `delete` op; rename to `[Archived] <name>` |

State-marker suffix grammar for topic titles:

| State | Suffix |
|---|---|
| `running` | ` · ▶` |
| `awaiting_input` | ` · ⏸` |
| `rate_limited` | ` · ⏳` |
| `stopped` | ` · ⏹` |
| `completed` | ` · ✅` |
| `error` | ` · ❌` |
| `deleted` | `[Archived] <name>` (replaces prefix; no suffix) |

---

## 5. DeliveryHeader (ephemeral, computed at render time)

See contracts/delivery-header.md for the exact grammar. Fields used to compute:

| Computed field | Source |
|---|---|
| icon | Task type (`🔄` for continuous) |
| name | `task.display_name` or `task.name` |
| step_counter | `current_step.number` / optional `program.total_steps` |
| state_label | derived from `task.status` (see §4 mapping) |
| hh_mm | `datetime.now(user_tz)` at render |
| next_step_preview | `next_step.description` truncated to 80 chars if present |

---

## 6. OrphanIncidentPayload (content of a single `orphan_incident` event's `payload`)

| Field | Type | Purpose |
|---|---|---|
| `last_exit_code` | `int | None` | From subprocess reap, if available |
| `last_output_tail` | `str` | Last 500 bytes of the step's `output.log` |
| `lock_last_heartbeat_ts` | `str (ISO-8601 UTC) | None` | For diagnostic context |
| `detected_cycles` | `int` | Always 3 at incident time (orphan_detect_count triggering the incident) |
| `dedicated_thread_id` | `int | None` | For cross-reference |

---

## 7. Relationships

```
ContinuousTask ─ 1 : 1 ─ DedicatedTopic          (dedicated_thread_id)
ContinuousTask ─ 1 : N ─ EventJournalEntry       (task_name foreign key; no DB constraint — journal is append-only log)
ContinuousTask ─ 1 : 1 ─ LockFile                (on-disk sibling)
```

No new indexes; queries are bounded by time window (journal) or direct path lookup (state / lock).

---

## 8. Migration (`v0_26_0.py`)

Applies once per install. See research.md R8 for the procedure. Data-model changes:
1. Introduces all "new" fields listed in §1 (default-populated).
2. Creates `data/events.jsonl` (empty) and `data/events/` directory.
3. Writes one `migration` event per existing continuous task.
4. Writes `migrated_v0_26_0` timestamp on each migrated state.

Idempotency: re-running the migration on an already-migrated install is a no-op on every per-task check (timestamp gate).

Rollback: if the release is reverted to v0.25.x, old code reads the extended state.json and ignores unknown fields; no data loss. The dedicated topic remains as a created-but-unused topic.
