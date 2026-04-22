# Contract — Lock File Heartbeat & Stale Detection

## Current lock-file format (pre-v0.26)

```
<pid>
<iso8601_creation_ts>
```

## New lock-file format (v0.26+)

Same two-line structure. Semantics of line 2 change from one-shot creation timestamp to continuously refreshed heartbeat:

```
<pid>
<iso8601_heartbeat_ts>
```

Every subprocess running a continuous-task step MUST refresh line 2 every **30 seconds** while alive. Refresh uses the existing atomic primitive:

```python
def refresh_heartbeat(lock_path: Path, pid: int) -> None:
    tmp = lock_path.with_suffix(".lock.tmp-<pid>")
    tmp.write_text("%d\n%s\n" % (pid, datetime.now(timezone.utc).isoformat()))
    tmp.replace(lock_path)
```

## Heartbeat loop (subprocess side)

Injected into the continuous-step subprocess entry point (`bot/continuous_worker.py` or the dispatch harness):

```python
import threading, time, signal

def _heartbeat_loop(lock_path: Path, pid: int, stop_event: threading.Event) -> None:
    # First refresh immediately; then every 30s until asked to stop.
    while not stop_event.is_set():
        try:
            refresh_heartbeat(lock_path, pid)
        except Exception as exc:
            logger.warning("heartbeat refresh failed: %s", exc)
        stop_event.wait(timeout=30.0)
```

Started as a daemon thread at subprocess entry; stop_event is set on normal exit and on SIGTERM.

## Stale-detection contract (scheduler side)

```python
def check_lock(task_name: str) -> LockStatus
    """Returns one of:
      - LockStatus.ALIVE           — pid running AND (heartbeat within 5 min OR
                                     legacy-format lock without heartbeat line)
      - LockStatus.STALE_DEAD_PID  — pid no longer exists on OS
      - LockStatus.STALE_ZOMBIE    — pid exists but heartbeat > 5 min old
      - LockStatus.MISSING         — no lock file
    """
```

Recovery matrix:

| Status | Scheduler action |
|---|---|
| ALIVE | Skip dispatch this cycle (task busy) |
| STALE_DEAD_PID | Delete lock, mark state orphan_candidate, next cycle can dispatch |
| STALE_ZOMBIE | Attempt `os.kill(pid, SIGTERM)` + wait 5 s + `SIGKILL` if still present + delete lock + journal `lock_recovered` event with reason="zombie" |
| MISSING | Normal: task not running; dispatch eligible |

## Orphan detection (status=running but MISSING lock)

Independent of heartbeat: if state says `running` and lock is `MISSING`, this is an orphan. Existing scheduler logic (`scheduler.py:1151-1160`) is extended:

1. Increment `task.orphan_detect_count` and set `task.orphan_last_detected_ts`.
2. If `orphan_detect_count < 3`: journal `orphan_detected` (minimal), silently mark state failed, scheduler may re-dispatch (a fresh step will reset the counter via `orphan_recovery`).
3. If `orphan_detect_count >= 3` AND detections are consecutive (each within 2× scheduler cycle): trigger **orphan incident**:
   - Read last 500 bytes of the task's `output.log`.
   - Journal `orphan_incident` event with full `OrphanIncidentPayload` (see data-model.md §6).
   - Transition `status → error`.
   - Post one structured incident message to the task's dedicated topic.
   - Stop emitting further orphan warnings for this task until it is reset (resumed or recreated).
4. Successful `orphan_recovery`: on next successful `step_start`, reset `orphan_detect_count = 0`, journal `orphan_recovery` event.

Consecutiveness check: if `now - orphan_last_detected_ts > 2 × SCHEDULER_CYCLE_SECONDS` (i.e. a cycle was missed or the task recovered briefly), reset the counter to 1 (treat as a fresh detection).

## Configuration knobs

Environment-driven with sensible defaults:

| Env var | Default | Purpose |
|---|---|---|
| `LOCK_HEARTBEAT_INTERVAL_SECONDS` | 30 | Subprocess refresh cadence |
| `LOCK_STALE_THRESHOLD_SECONDS` | 300 | Age beyond which a heartbeat is stale |
| `ORPHAN_INCIDENT_THRESHOLD` | 3 | Consecutive detections that trigger an incident |

Exposed for tests to shorten (e.g. 1 s / 3 s / 2) without touching the production defaults.

## Backward compatibility

Existing locks without a heartbeat line (file with pid only, or second line that is the original creation timestamp from v0.25.x and earlier) are handled:

- **pid-only locks** (one-line): treated as alive while the pid is running; treated as stale the moment the pid dies (per today's behaviour).
- **Legacy timestamp locks** (two-line with old meaning): if the subprocess has not been restarted under the new version, the second line stays "fresh" from the creation moment. These locks migrate naturally: once the subprocess terminates normally under the new code, the new lock format is written by the next dispatch. Mixed-format transitional state is safe.

## Journal integration

Every lock state change journals:

| Condition | Event type | outcome |
|---|---|---|
| First heartbeat refresh failure | `lock_heartbeat_stale` | `refresh_failed` |
| Stale lock cleaned by scheduler | `lock_recovered` | `stale_dead_pid` / `stale_zombie` |
| Orphan incident triggered | `orphan_incident` | `escalated` |
| Orphan recovery | `orphan_recovery` | `cleared` |
