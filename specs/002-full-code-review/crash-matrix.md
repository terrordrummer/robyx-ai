# Crash-Survival Matrix — Pass 2 T064

**Date**: 2026-04-16
**Source**: `research.md` §R2 expanded with current-behavior verification
against the codebase at HEAD of `002-full-code-review`.

Each row is a scenario, its current behavior (verified or assumed), the
target behavior, and the Phase 10 task that closes the gap.

## Legend

- **Current**: `verified` (read the code) · `assumed` (inferred but not
  read) · **bold** when a gap is identified.

---

## C1 — SIGKILL mid-write `data/queue.json`

- **Current (verified)**: `scheduler.py` lines 137–142 write to
  `QUEUE_FILE.with_suffix(".tmp")` then `os.replace(tmp, QUEUE_FILE)`. Atomic
  on POSIX; either the old file or the new file is present, never a partial.
- **Target**: same as current.
- **Gap**: parent-directory `fsync` is NOT called. On ext4 `data=writeback` or
  crash immediately after `replace`, the directory entry may not be durable.
  Belt-and-braces fix: `fd = os.open(parent_dir, os.O_RDONLY); os.fsync(fd)`
  after each replace.
- **Task**: T080 addendum (optional fsync hardening).

## C2 — SIGKILL mid-migration

- **Current (verified)**: `migrations/runner.py` walks migrations from
  `tracker.current_version` forward. Steps that modify state must be
  idempotent by convention (per module contract). Version is advanced by
  `tracker.record_step` — confirm it uses `tmp+replace`.
- **Target**: if a migration is interrupted, re-running the runner picks up
  at the same version and re-runs the step safely (idempotent).
- **Gap**: no per-step atomicity — a migration that writes to multiple files
  may leave them inconsistent if SIGKILL lands between writes. Phase 10 T088
  audits each migration for *all-or-nothing* writes (batch tmp files into a
  single transactional commit via `os.replace` ordering).
- **Task**: T088.

## C3 — AI subprocess stalls past timeout

- **Current (assumed)**: `ai_invoke.py` has timeout handling (Pass 1 F06
  touched this path). Orphan tracker exists at `orphan_tracker.py`.
- **Target**: subprocess killed, orphan tracker sees PID even on parent
  crash between kill and cleanup.
- **Gap**: verify `finally` block registers PID in tracker BEFORE launching
  subprocess, not after (Pass 1 F07 partially addressed this — re-verify).
- **Task**: T082.

## C4 — Disk full on `data/` write

- **Current (assumed)**: `OSError: [Errno 28]` not specially handled; bubbles
  up from `write_text`.
- **Target**: user-facing message "Cannot save state: disk full — free some
  space and retry". No existing state file corruption (tmp+replace already
  prevents this — the old file survives).
- **Gap**: no explicit catch for `OSError` with errno 28 anywhere.
- **Task**: T080/T085 — wrap atomic-write helpers with `OSError` catch and
  a user-surfaced message via `i18n`.

## C5 — Corrupted `data/agents.json` at startup

- **Current (verified)**: 8 modules (`config.py`, `orphan_tracker.py`,
  `ai_invoke.py`, `scheduler.py`, `updater.py`, `task_runtime.py`,
  `continuous.py`, `ai_backend.py`) have `except JSONDecodeError`. Verify
  `agents.py` and `collaborative.py` do the same — Pass 1 F17 flagged
  `collaborative.py` as deferred.
- **Target**: load-time validator with backup-and-rebuild path; corrupted
  file renamed to `agents.json.corrupt-<timestamp>`, bot starts with empty
  registry, user informed.
- **Gap**: `agents.py` load path not yet verified; `collaborative.py` still
  on deferred list (F17).
- **Task**: T085 (closes F17 + audits `agents.py`).

## C6 — Clock NTP jump backward

- **Current (verified)**: `scheduler.py` uses neither `time.time()` nor
  `time.monotonic()` directly — it uses `datetime` (wall-clock). A backward
  NTP step could cause late tasks to fire twice or miss firing.
- **Target**: use `time.monotonic()` for all *interval* computations
  (dedup windows, retry backoff). Wall-clock is fine for *deadlines* that
  users see ("fire at 9pm"); monotonic for durations ("retry in 30s").
- **Gap**: all scheduler interval logic uses wall-clock.
- **Task**: T080 — dedicated audit + fix.

## C7 — Restart storm (10 crashes/min)

- **Current (assumed)**: systemd/launchd restart; PID lock from C8 below.
- **Target**: state survives C1 fix (`tmp+replace`); no leaked tempfiles
  accumulate.
- **Gap**: verify `data/*.tmp` cleanup on startup (stale tmp files from
  previous crashed writes should be deleted at boot).
- **Task**: T081.

## C8 — Two bot processes racing on `bot.pid`

- **Current (verified — CRITICAL GAP)**: `bot.py` lines 108–126 use a
  **TOCTOU pattern**: `if PID_FILE.exists(): ... read ... else: ... write`.
  Two processes can BOTH pass the `exists()` check and both write their PID,
  resulting in two running instances. Meanwhile `scheduler.py:98` and
  `collaborative.py:153` both use proper `fcntl.flock(fd, fcntl.LOCK_EX)` —
  the pattern IS in the codebase, just not used here.
- **Target**: POSIX advisory file lock via `fcntl.flock` on an opened file
  descriptor held for the lifetime of the process.
- **Gap**: the classic one.
- **Task**: T081 — **expected finding ID P2-20 (High, bot.py)**.

## C9 — AI CLI binary missing

- **Current (assumed)**: `ai_backend.py` detects binary at import/invoke;
  raises if absent.
- **Target**: user sees a conversational message via `i18n` explaining what
  to install, not a Python traceback.
- **Task**: T094 (UX lens).

## C10 — `data/memory.db` locked by another process

- **Current (assumed)**: SQLite `OperationalError: database is locked` could
  bubble up.
- **Target**: retry with exponential backoff (200 ms × 2^n up to 3 s); after
  limit, surface to user with actionable message.
- **Task**: T077 (security lens covers parameterization; stability lens
  handles retry — could split but current plan folds it under T077).

## C11 — Continuous task partial step

- **Current (assumed)**: `continuous.py` writes progress per step; Pass 1
  reviewed this module. Need to confirm the write is `tmp+replace`.
- **Target**: step boundary atomic; resume-from-last-committed-step on
  restart.
- **Task**: T084.

## C12 — `.env` hot-edited while running

- **Current (assumed)**: `config_updates.py` watches `.env`. No explicit
  mutex against ongoing AI invocations.
- **Target**: reload gated by a mutex; pending AI calls continue with
  old secrets; new calls pick up new secrets.
- **Task**: T074 — **expected finding ID P2-13 (Med, config_updates.py)**.

---

## Summary of gaps (high-severity candidates for Phase 10)

Ranked by severity:

1. **C8 — PID lock TOCTOU race** (verified critical gap). Expected ID
   **P2-20 (High, bot.py)**.
2. **C6 — wall-clock used for scheduler intervals** (verified). Expected ID
   **P2-21 (Med, scheduler.py)**.
3. **C2 — migration per-step atomicity** (to verify per-migration). Expected
   ID(s) **P2-22…** (one per migration if gap confirmed).
4. **C5 — JSON-corruption recovery not uniform** (Pass 1 F17 still open,
   plus `agents.py` unverified). Expected ID **P2-23**.
5. **C1 — parent-dir `fsync` missing** (belt-and-braces; lower priority).
6. **C12 — `.env` hot-reload mutex** (overlaps with Phase 9 X-3). Expected ID
   **P2-13**.

Other rows to be verified during Phase 10 — current-behavior field marked
"assumed" means reviewer must read the code and confirm before filing or
dismissing the finding.
