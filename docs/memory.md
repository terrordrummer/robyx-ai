# Memory System

← [Back to README](../README.md)

Agents need to remember context between conversations. Robyx has a
two-tier memory system backed by SQLite with FTS5 full-text search
(introduced in v0.21.0; migrated automatically from the markdown-based
engine used through v0.20.28).

## Two tiers

- **Active memory** — a compact snapshot (~5 000 words max) loaded into
  the agent's context at the start of every conversation. One row per
  agent, keyed by name. Contains the current state of the project/task,
  active decisions and their reasoning, open TODOs, and known gotchas.
- **Archive** — an append-only log of entries that are no longer
  current but may be relevant later. Stored in the same database,
  indexed for full-text search, queryable on demand (not loaded into
  every context).

Agents update their active memory **continuously**, not at session
boundaries. A decision is made → write it now. A TODO is completed →
update immediately. When an entry becomes obsolete, the agent moves it
from the active snapshot to the archive with a short `archive_reason`.

## Storage backend

Each agent gets its own SQLite database file:

- **Robyx (orchestrator)**: `data/memory/robyx.db`
- **Specialists**: `data/memory/<name>.db`
- **Workspace agents without native memory**: `<work_dir>/.robyx/memory.db`
  — lives next to the project, not under Robyx's `data/`, so the
  memory moves with the repo if the workspace is cloned or relocated.
- **Workspace agents with native memory** (CLAUDE.md or `.claude/` in
  the project): *none* — Robyx stays out of the way, the AI backend
  uses the project's native memory natively.

Each database has two tables:

- `active_snapshots (agent_name PK, content, word_count, updated_at)`
  — single-row-per-agent active memory.
- `entries (id PK, agent_name, tier, content, topic, tags,
  created_at, archived_at, archive_reason)` — the archive.
  Mirror `entries_fts` (FTS5 virtual table) powers the archive search.

Crash safety:

- WAL mode is enabled at connection open (`PRAGMA journal_mode=WAL`).
  Concurrent readers don't block the single writer; a crash mid-write
  leaves the WAL intact and SQLite replays on next open.
- Every write uses a parameterized statement inside an implicit
  transaction — there is no partial-row state an agent can observe.

## Integration with existing projects

Robyx respects each project's existing setup:

| Project state | Memory behavior |
|---------------|-----------------|
| Has Claude Code memory (`CLAUDE.md` and/or `.claude/`) | Robyx does **not** create a `.db` — native memory works as-is. |
| No existing memory | Robyx creates `<work_dir>/.robyx/memory.db` with active + archive — the DB lives inside the workspace project, not under Robyx's `data/`. |
| Robyx orchestrator and specialists | Always use `data/memory/{name}.db` (centralized under Robyx's own `data/`). |

This means you can work on a project **both directly** (terminal +
Claude Code) **and via Robyx** (chat) without memory conflicts.

## Searching the archive

Agents (and their AI backend) can query the archive by keyword or
topic. The FTS5 index covers `content`, `topic`, and `tags`, with
BM25 ranking and snippet highlighting. Results return the most
relevant archived entries for the query, regardless of how long ago
they were written.

## Migration from markdown (v0.20.x → v0.21.0)

Pre-0.21.0 memory was plain markdown (`active.md` + quarterly
archives under `archive/`). The v0.21.0 release includes migration
`bot/migrations/v0_21_0.py`, which:

1. Scans `data/memory/robyx/` and every specialist directory for
   existing `active.md` and archive files.
2. Creates the corresponding `.db` with the two-table schema.
3. Imports the active snapshot and every archive entry
   (preserving topics, timestamps, archive reasons where present).
4. Renames the original markdown files to `*.md.bak` so nothing is
   lost — you can delete the `.bak` files once you've verified the
   migration worked.

The migration is idempotent. Running it a second time on an
already-migrated memory dir is a no-op.

---

← [Back to README](../README.md)
