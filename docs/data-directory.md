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
│       └── history/
├── <task-name>/             # Per-scheduled-task runtime artifacts
│   ├── lock                 # PID lock during subprocess run
│   └── output.log           # Captured stdout+stderr of last run
├── backups/                 # Pre-update tar+gz snapshots of data/
│   └── pre-update-<from>-to-<to>-<ts>.tar.gz
└── bot.log                  # Rotating log file
```

## File contract

| File | Writer | Safe to delete? | Notes |
|------|--------|-----------------|-------|
| `state.json` | `bot/agents.py` | No — agent registry is lost (thread_ids, session_ids, focus). Recreate by recreating every workspace. | Atomic writes via `tmp + os.replace`. |
| `queue.json` | `bot/scheduler.py` | Only if no task is in-flight. Safer to wait for all `status=running` to finish and then delete. Pending reminders and periodic tasks are lost but not harmful. | All mutations go through `_queue_mutex()` (thread + POSIX file lock). |
| `queue.json.lock` | — (empty sidecar) | Yes, anytime. Recreated on next mutation. | Only used as `fcntl.flock` target. |
| `bot.pid` | `bot/bot.py` | Yes when the bot is not running. Overwritten on every start. | Written after the single-instance lock is acquired. Informational only — the actual mutual-exclusion comes from `bot.pid.lock`. |
| `bot.pid.lock` | — (empty sidecar, v0.21.0+) | Yes when the bot is not running. Recreated on next start. | Sidecar file that holds a POSIX `fcntl.LOCK_EX \| LOCK_NB` for the life of the process. The kernel releases the lock automatically on process exit (even SIGKILL), so a crashed owner never keeps the lock stuck. Non-POSIX platforms fall back to the pre-0.21.0 PID-file check. |
| `active-pids.json` | `bot/orphan_tracker.py` | Yes. Registry is rebuilt from every new spawn; on next boot `cleanup_on_startup()` will only see PIDs it re-registered. | Atomic writes. |
| `updates.json` | `bot/updater.py` | Yes. Auto-updater will check GitHub again on next tick. | — |
| `migrations.json` | `bot/migrations/tracker.py` | **No.** Deletion causes every migration in the chain to re-run on next boot, which may re-apply fixes that have since been superseded. If it must be reset, restore from a `backups/` snapshot. | — |
| `tasks.md`, `specialists.md` | Legacy (pre-0.20) | Yes, if you have already run the migration to the unified queue. | Kept read-only for the migration runner. |
| `agents/*.md`, `specialists/*.md` | `bot/topics.py`, chat | Deleting a brief removes the agent's instructions; the agent still exists in `state.json` but will fall back to the base role prompt. Regenerate via `/reset` + describe the role again to Robyx. | — |
| `memory/*.db` | `bot/memory_store.py`, agents themselves | Yes per file — the affected agent loses long-term context but keeps current session. Active snapshot and full archive live in the same DB; delete one `.db` to reset just that agent. | SQLite with WAL journal; each file also has sidecar `-wal` and `-shm` files that SQLite manages automatically. Only the orchestrator (`robyx.db`) and specialists live here. Workspace agents without native memory store their DB at `<work_dir>/.robyx/memory.db` inside the workspace itself, so it travels with the project; workspaces whose project has a native `CLAUDE.md` or `.claude/` use that instead and do NOT get a `.db`. See [docs/memory.md](memory.md). |
| `collaborative_workspaces.json` | `bot/collaborative.py` | No — deletion loses every collaborative-workspace registration (chat IDs, roles, interaction mode). Rebuild by re-adding the bot to each group. | Atomic writes via `tmp + os.replace`; `fcntl.flock` cross-process mutex since v0.20.28 (with `msvcrt` fallback on Windows). |
| `*.corrupt-<UTC-timestamp>` | `bot/agents._quarantine_corrupt_file` (v0.21.1+) | Yes — forensic copies of JSON state files that failed to decode at load time (`state.json.corrupt-20260416T173805Z`, `collaborative_workspaces.json.corrupt-…`, etc.). Bot starts with empty state; the original bytes are preserved for operator inspection. Safe to delete once the cause has been investigated or the data is no longer needed. | Created when `state.json` or `collaborative_workspaces.json` fails `json.loads`/UTF-8 decode on startup. Prevents the next save from silently overwriting corrupt data. |
| `continuous/<name>/state.json` | `bot/continuous.py` | **No** — deleting mid-task orphans the continuous task. Close the workspace via Robyx first (which cancels queue entries and archives the branch) before cleaning up. | Atomic writes. |
| `<task-name>/lock` | `bot/scheduler.py` | Yes, only if no subprocess is holding the PID. `check_lock()` + `cleanup_stale_locks_on_startup()` clean these automatically. | — |
| `<task-name>/output.log` | `bot/scheduler.py` | Yes — purely for post-mortem inspection. Overwritten on every dispatch. | — |
| `backups/*.tar.gz` | `bot/updater.py` | Yes — older than the 3 most recent are automatically pruned. Keep at least one snapshot if you intend to roll back. | Excludes `backups/` itself to avoid recursive growth. |
| `bot.log` | logging | Yes. Python's `RotatingFileHandler` caps it automatically. | — |

## Backup and recovery

The auto-updater takes a `tar+gzip` snapshot of `data/` (excluding
`backups/`) before every self-update and keeps the three most recent.
See [docs/updates.md](updates.md) for the full update flow.

If you need to roll back manually:

```bash
cd <robyx-repo>/data
tar -xzf backups/pre-update-<from>-to-<to>-<ts>.tar.gz
```

Snapshots contain every file listed above except `backups/` itself, so
a restore replaces agent registry, queue, memory, continuous-task
state, and migration history in one atomic operation.

For disaster recovery outside the updater flow (manual corruption,
disk failure), a cold backup of `data/` taken while the bot is
**stopped** is the safest restore source. Restoring a hot backup may
race with in-flight queue mutations.

## What is *not* stored here

- **Bot source code** — in `bot/`, managed by git.
- **Templates / prompts** — in `templates/`, managed by git.
- **Configuration** — in `.env` at the repo root. Not gitignored by
  default — take care if you commit your workspace.
- **Python venv** — wherever you created it (usually `.venv/`). The
  bootstrap re-installs dependencies from `bot/requirements.txt` on
  every boot if the hash changes (see [docs/updates.md](updates.md)).

---

← [Back to README](../README.md)
