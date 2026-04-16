# Implementation Plan: Memory Engine Evolution

**Branch**: `001-memory-engine-analysis` | **Date**: 2026-04-16 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/001-memory-engine-analysis/spec.md`

## Summary

Replace Robyx's markdown-based agent memory system with a SQLite-backed engine
using FTS5 for full-text search. The current system (flat markdown files with no
indexing) does not scale beyond a few hundred entries. SQLite + FTS5 provides
ACID crash safety, sub-millisecond active memory loads, and <10ms archive queries
on 10,000+ entries — with zero new dependencies (sqlite3 is in Python's stdlib).
An optional sqlite-vec vector search layer can be added in Phase 2 if semantic
retrieval proves necessary.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: sqlite3 (stdlib), optionally sqlite-vec (~165KB)
**Storage**: SQLite databases under `data/memory/` (per-agent `.db` files)
**Testing**: pytest (existing test suite in `tests/`)
**Target Platform**: macOS, Linux, Windows
**Project Type**: Python application (messaging bot / agent orchestrator)
**Performance Goals**: Active memory load <500ms (actual: <1ms), archive query <2s (actual: <10ms on 10K entries)
**Constraints**: No external DB servers, self-contained within Python runtime, <50MB storage per agent per year
**Scale/Scope**: Single-user deployment, 1-20 agents, up to 10K archived entries per agent

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Multi-Platform Parity | ✅ PASS | Memory engine is backend-agnostic — memory is injected as text into prompts regardless of messaging platform |
| II. Chat-First Configuration | ✅ PASS | Memory management remains chat-driven. No new config files required. |
| III. Resilience & State Persistence | ✅ PASS | SQLite WAL mode provides ACID atomicity — strict improvement over current file I/O |
| IV. Comprehensive Testing | ✅ PASS | Existing `tests/test_memory.py` will be updated; new tests for FTS5, migration, crash recovery |
| V. Safe Evolution | ✅ PASS | Migration via `bot/migrations/` framework. Markdown files backed up as `.md.bak`. Rollback possible. |

No violations. No complexity justification needed.

## Project Structure

### Documentation (this feature)

```text
specs/001-memory-engine-analysis/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 research output
├── data-model.md        # Entity model and schema
├── quickstart.md        # Verification guide
├── contracts/
│   └── memory-api.md    # Public API contract
└── tasks.md             # Task list (created by /speckit.tasks)
```

### Source Code (repository root)

```text
bot/
├── memory.py            # MODIFIED — core rewrite (file I/O → SQLite)
├── memory_store.py      # NEW — SQLite storage layer (schema, queries, FTS5)
├── migrations/
│   └── v0_21_0.py       # NEW — markdown-to-SQLite migration
tests/
├── test_memory.py       # MODIFIED — updated for new API signatures
├── test_memory_store.py # NEW — SQLite store unit tests
```

**Structure Decision**: No new directories. Two new files (`memory_store.py`,
migration), two modified files (`memory.py`, `test_memory.py`). The module
boundary stays within `bot/` — memory is not extracted into a separate package.

## Complexity Tracking

> No Constitution Check violations. No complexity justification needed.

## Phase Summary

| Phase | Deliverable | Key Risk |
|-------|-------------|----------|
| Phase 0 | research.md | ✅ Complete — SQLite+FTS5 selected |
| Phase 1 | data-model.md, contracts/, quickstart.md | ✅ Complete |
| Phase 2 | tasks.md (via `/speckit.tasks`) | Next step |

## Implementation Strategy

### Phase 1: SQLite + FTS5 Core (zero new dependencies)

1. Create `bot/memory_store.py` with SQLite storage layer:
   - Schema creation (entries, active_snapshots, FTS5 virtual table)
   - WAL mode configuration
   - CRUD operations for active memory and archive entries
   - FTS5 search with BM25 ranking

2. Rewrite `bot/memory.py` internals:
   - Replace `Path.read_text()` / `open().write()` with `memory_store` calls
   - Keep public API surface stable (same function names, updated signatures)
   - Add `search_archive()` as new capability
   - Update `MEMORY_INSTRUCTIONS` to reference search capability

3. Write migration `bot/migrations/v0_21_0.py`:
   - Parse existing `active.md` files → INSERT into `active_snapshots`
   - Parse existing `archive/YYYY-QN.md` files → split by `---` → INSERT entries
   - Rename old files to `.md.bak`
   - Idempotent (re-running is a no-op)

4. Update tests:
   - Adapt `tests/test_memory.py` for new signatures
   - Add `tests/test_memory_store.py` for SQLite layer
   - Add crash-recovery test (write, simulate crash, verify consistency)
   - Add FTS5 search quality tests

### Phase 2: Optional Vector Search (future, NOT in this release)

- Gate behind sqlite-vec availability (try-import)
- Add embedding generation using API-based embeddings
- Hybrid retrieval: FTS5 candidate set, re-ranked by vector similarity
- Separate feature branch when needed

## Key Decisions

1. **SQLite per agent** (not one shared DB): Isolation by default, no locking
   contention between agents, easy backup/deletion per agent.

2. **Active memory as a single-row table** (not decomposed into entries):
   The active memory blob is what gets injected into LLM context. Decomposing
   it into individual entries would require a reconstruction step and complicate
   the "agent maintains its own active.md" mental model.

3. **FTS5 over basic LIKE queries**: BM25 ranking provides relevance scoring,
   prefix/phrase matching handles natural language queries well, and FTS5 is
   included in Python's bundled SQLite.

4. **Migration via existing framework**: Uses `bot/migrations/v0_21_0.py` in the
   established migration chain, consistent with Constitution Principle V.
