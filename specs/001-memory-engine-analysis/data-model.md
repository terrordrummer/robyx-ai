# Data Model: Memory Engine

**Date**: 2026-04-16
**Feature**: Memory Engine Evolution

## Entities

### MemoryEntry

A discrete unit of memory — a decision, TODO, state snapshot, or observation.

| Field        | Type     | Description                                             |
|--------------|----------|---------------------------------------------------------|
| id           | INTEGER  | Auto-increment primary key                              |
| agent_name   | TEXT     | Agent that owns this entry (e.g., "robyx", "deploy-bot")|
| tier         | TEXT     | "active" or "archive"                                   |
| content      | TEXT     | The memory content (markdown-formatted text)            |
| topic        | TEXT     | Primary topic tag (e.g., "auth", "deployment", "config")|
| tags         | TEXT     | Comma-separated secondary tags                          |
| created_at   | TEXT     | ISO 8601 timestamp of creation                          |
| archived_at  | TEXT     | ISO 8601 timestamp when archived (NULL if active)       |
| archive_reason | TEXT   | Why this entry was archived (e.g., "obsolete", "completed") |

### ActiveSnapshot

The consolidated active memory blob for an agent — what gets loaded into LLM context.

| Field        | Type     | Description                                             |
|--------------|----------|---------------------------------------------------------|
| agent_name   | TEXT     | Primary key — one snapshot per agent                    |
| content      | TEXT     | Full active memory text (≤5000 words)                   |
| word_count   | INTEGER  | Cached word count for budget checks                     |
| updated_at   | TEXT     | ISO 8601 timestamp of last update                       |

### FTS Index (Virtual Table)

FTS5 virtual table for full-text search across archived entries.

| Column       | Source       | Description                                          |
|--------------|--------------|------------------------------------------------------|
| content      | entries.content | Searchable memory text                            |
| topic        | entries.topic   | Searchable topic tag                              |
| tags         | entries.tags    | Searchable secondary tags                         |

### VectorIndex (Optional — Phase 2)

sqlite-vec virtual table for semantic search. Only created when sqlite-vec is available.

| Column       | Type          | Description                                        |
|--------------|---------------|----------------------------------------------------|
| entry_id     | INTEGER       | FK to entries.id                                   |
| embedding    | FLOAT[384]    | Vector embedding of content (dimension depends on model) |

## Relationships

- **ActiveSnapshot ↔ Agent**: 1:1 — each agent has exactly one active snapshot.
- **MemoryEntry ↔ Agent**: N:1 — each agent has many entries.
- **FTS Index ↔ MemoryEntry**: 1:1 shadow — FTS5 content table mirrors entries.
- **VectorIndex ↔ MemoryEntry**: 1:1 optional — only populated when vectors enabled.

## State Transitions

```
┌─────────┐     save_active()     ┌────────────┐
│  (new)   │ ──────────────────── │   active    │
└─────────┘                       └──────┬─────┘
                                         │ archive_entry()
                                         ▼
                                  ┌────────────┐
                                  │  archived   │
                                  └────────────┘
```

- Active entries can be updated in place (overwrite snapshot).
- When active memory exceeds budget, oldest/least-relevant entries move to archive.
- Archive entries are append-only — never modified after archiving.

## Storage Layout

```
data/memory/
├── robyx.db              # Orchestrator memory (SQLite)
├── deploy-bot.db         # Specialist memory (SQLite)
└── ...

{work_dir}/.robyx/
└── memory.db             # Workspace agent memory (SQLite)
```

Each `.db` file is a self-contained SQLite database with WAL mode enabled.
Replaces the previous structure of `active.md` + `archive/` directories.

## Migration from Markdown

The migration function parses existing files and inserts them:

1. Read `active.md` → INSERT into `active_snapshots`
2. Read each `archive/YYYY-QN.md` → split by `---` separator → INSERT each
   entry into `entries` with tier="archive"
3. Rename old files to `.md.bak` (reversible)
4. Mark migration as applied in `data/migrations.json`
