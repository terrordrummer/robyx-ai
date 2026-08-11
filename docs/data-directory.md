# Data directory contract

← [Back to README](../README.md)

All of Robyx's runtime state lives under `data/` in the repository root.
The directory is gitignored so personal data, session UUIDs, and API
snapshots are never committed. This page documents what lives there,
who writes it, what is safe to delete, and how recovery works.

## Layout

```
data/
├── state.json               # AgentManager: agent registry + focus
├── queue.json               # Unified scheduler queue (reminders + tasks)
├── queue.json.lock          # fcntl sidecar for cross-process queue mutex
├── bot.pid                  # PID of the running bot (for introspection)
├── bot.pid.lock             # fcntl sidecar for single-instance lock (v0.21.0+)
├── active-pids.json         # Orphan tracker: PIDs the bot spawned
├── updates.json             # Auto-updater: last-check + pending tag
├── migrations.json          # Migration framework: applied versions
├── tasks.md                 # Legacy tasks source (pre-unified queue)
├── specialists.md           # Legacy specialists source
├── agents/                  # Per-workspace agent briefs (*.md)
├── specialists/             # Per-specialist briefs (*.md)
├── memory/                  # Centralized memory — SQLite databases (v0.21.0+)
│   ├── robyx.db             # Orchestrator memory
│   └── <specialist>.db      # One DB per specialist
│                            # (workspace memory lives in the workspace
│                            #  itself at <work_dir>/.robyx/memory.db,
│                            #  NOT here — see docs/memory.md)
├── collaborative_workspaces.json   # Collaborative workspace registry
├── continuous/              # Per-continuous-task state + logs
│   └── <name>/
│       ├── state.json
│       ├── program.json     # Revisioned intent + accepted plan authority
│       ├── plan.md
│       └── history/
├── <task-name>/             # Per-scheduled-task runtime artifacts
│   ├── lock                 # PID lock during subprocess run
│   └── output.log           # Captured stdout+stderr of last run
├── backups/                 # Pre-update tar+gz snapshots of data/
│   ├── active-update.json    # Durable in-progress update/recovery marker
│   └── pre-update-<from>-to-<to>-<ts>.tar.gz
└── bot.log                  # Rotating log file
```

## File contract

| File | Writer | Safe to delete? | Notes |
|------|--------|-----------------|-------|
| `state.json` | `bot/agents.py` | No — agent registry is lost (thread_ids, session_ids, focus). Recreate by recreating every workspace. | Atomic writes; full schema validation, quarantine/snapshot recovery, and fail-closed mutation. |
| `queue.json` | `bot/scheduler.py` | Only if no task is in-flight. Safer to wait for all `status=running` to finish and then delete. Pending reminders and periodic tasks are lost. | All mutations go through `_queue_mutex()` (thread + POSIX file lock). Corruption is quarantined and recovered from the newest verified updater snapshot; without one, reads and mutations fail closed. |
| `queue.json.lock` | — (empty sidecar) | Yes, anytime. Recreated on next mutation. | Only used as `fcntl.flock` target. |
| `bot.pid` | `bot/bot.py` | Yes when the bot is not running. Overwritten on every start. | Written after the single-instance lock is acquired. Informational only — the actual mutual-exclusion comes from `bot.pid.lock`. |
| `bot.pid.lock` | — (empty sidecar, v0.21.0+) | Yes when the bot is not running. Recreated on next start. | Sidecar file that holds a POSIX `fcntl.LOCK_EX \| LOCK_NB` for the life of the process. The kernel releases the lock automatically on process exit (even SIGKILL), so a crashed owner never keeps the lock stuck. Non-POSIX platforms fall back to the pre-0.21.0 PID-file check. |
| `active-pids.json` | `bot/orphan_tracker.py` | Only while the bot is stopped and no tracked child can still be alive. Deleting it discards orphan evidence. | Atomic writes; malformed/corrupt registries fail startup closed instead of being replaced with `{}`. |
| `updates.json` | `bot/updater.py` | Yes. Auto-updater will check GitHub again on next tick. | — |
| `migrations.json` | `bot/migrations/tracker.py` | **No.** Deletion causes every migration in the chain to re-run on next boot, which may re-apply fixes that have since been superseded. If it must be reset, restore from a `backups/` snapshot. | Full legacy+chain schema validation; corruption aborts startup and cannot be reseeded as an empty tracker. |
| `tasks.md`, `specialists.md` | Legacy (pre-0.20) | Yes, if you have already run the migration to the unified queue. | Kept read-only for the migration runner. |
| `agents/*.md`, `specialists/*.md` | `bot/topics.py`, chat | Deleting a brief removes the agent's instructions; the agent still exists in `state.json` but will fall back to the base role prompt. Regenerate via `/reset` + describe the role again to Robyx. | — |
| `memory/*.db` | `bot/memory_store.py`, agents themselves | Yes per file — the affected agent loses long-term context but keeps current session. Active snapshot and full archive live in the same DB; delete one `.db` to reset just that agent. | SQLite with WAL journal; each file also has sidecar `-wal` and `-shm` files that SQLite manages automatically. Only the orchestrator (`robyx.db`) and specialists live here. Workspace agents without native memory store their DB at `<work_dir>/.robyx/memory.db` inside the workspace itself, so it travels with the project; workspaces whose project has a native `CLAUDE.md` or `.claude/` use that instead and do NOT get a `.db`. See [docs/memory.md](memory.md). |
| `collaborative_workspaces.json` | `bot/collaborative.py` | No — deletion loses every collaborative-workspace registration (chat IDs, roles, interaction mode). Rebuild by re-adding the bot to each group. | Atomic locked writes plus whole-document validation, quarantine/snapshot recovery, and fail-closed mutation. |
| `*.corrupt-<UTC-timestamp>-<id>` | JSON state writers / recovery layer | Yes, after investigation — these are forensic copies of JSON files that failed decode or shape validation. | Every critical registry above uses verified snapshot recovery and fails closed without one. Never delete the only forensic copy before reconciliation. |
| `*.recovery-pending` | `bot/persistence_recovery.py` | No while present. | Durable marker for recovery interrupted between quarantine and atomic install. The next authoritative load resumes recovery instead of treating the live file as new. |
| `*.unavailable` | `bot/persistence_recovery.py` | No while present. | Blocks stale in-memory writes after corruption/recovery was first discovered by a mutator. Only a successful authoritative reload clears it. |
| `continuous/<name>/state.json` | `bot/continuous.py` | **No** — deleting mid-task orphans the continuous task. Use `[DELETE_TASK]` for intentional removal; closing the parent workspace only drains and stops its children and does not archive their git branches. | Atomic writes; corruption follows the same quarantine, verified-snapshot recovery, and fail-closed policy as `queue.json`. |
| `continuous/<name>/program.json` | `bot/continuous.py` | **No** — deleting it after revision 0 disables the task. | Revisioned authority for the program and accepted `plan.md` body. State loads overlay this record and repair an interrupted plan commit; corrupt or missing positive revisions fail closed. |
| `<task-name>/lock` | `bot/scheduler.py` | Yes, only if no subprocess is holding the PID. `check_lock()` + `cleanup_stale_locks_on_startup()` clean these automatically. | — |
| `<task-name>/output.log` | `bot/scheduler.py` | Yes — purely for post-mortem inspection. Overwritten on every dispatch. | — |
| `backups/*.tar.gz` | `bot/updater.py` | Yes — older than the 3 most recent are automatically pruned. Keep at least one snapshot if you intend to roll back. | Excludes `backups/` itself to avoid recursive growth. |
| `backups/active-update.json` | `bot/updater.py` | **No while present.** | Durable proof that an update crossed its first mutation. Startup must finish verified rollback/recovery and smoke validation before the marker is removed; deleting it can hide a partially applied code/data transaction. |
| `bot.log` | logging | Yes. Python's `RotatingFileHandler` caps it automatically. | — |

## Backup and recovery

The auto-updater takes a `tar+gzip` snapshot of `data/` (excluding
`backups/`) before every self-update and keeps the three most recent.
See [docs/updates.md](updates.md) for the full update flow.

If you need to roll back manually, stop the service first. Never extract a
snapshot over the live `data/` tree: tar extraction is not atomic and can
leave a mixed-generation registry. Extract into a sibling staging directory,
validate the archive and critical JSON there, then atomically rename the live
tree to a quarantine path and the staged tree to `data/`. Preserve the existing
`backups/` directory and the quarantined tree until the restarted bot passes
its recovery checks. The updater performs this staged transaction
automatically; prefer that path whenever it is available.

Snapshots contain every file listed above except `backups/` itself, so a
successful staged restore replaces agent registry, queue, memory,
continuous-task state, and migration history as one generation.

For disaster recovery outside the updater flow (manual corruption,
disk failure), a cold backup of `data/` taken while the bot is
**stopped** is the safest restore source. Restoring a hot backup may
race with in-flight queue mutations.

For corrupt critical JSON (`state.json`, `queue.json`, continuous state,
collaborative registry, migration tracker, or active PID registry), normal
loading first preserves the bad bytes as a uniquely named `.corrupt-*` file, then scans
`data/backups/pre-update-*.tar.gz` newest-to-oldest. A candidate is accepted only
when the exact member is a regular file below the recovery size limit and its
JSON has the expected top-level shape. The recovered file is installed
atomically. If every candidate is unusable, Robyx refuses to mutate the affected
state; it does not manufacture an empty replacement. Because a valid snapshot
may be older than current runtime state, inspect the CRITICAL log/event and
reconcile work created since that snapshot.

## What is *not* stored here

- **Bot source code** — in `bot/`, managed by git.
- **Templates / prompts** — in `templates/`, managed by git.
- **Configuration** — in `.env` at the repo root. It is gitignored and written
  privately, but still belongs in encrypted backups rather than source control.
- **Python venv** — wherever you created it (usually `.venv/`). The
  bootstrap selects the current-minor hash-verified runtime lock and
  re-installs dependencies when either that lock or `bot/requirements.txt`
  changes (see [docs/updates.md](updates.md)).

---

← [Back to README](../README.md)
