# Tasks: Memory Engine Evolution

**Input**: Design documents from `specs/001-memory-engine-analysis/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Test tasks are included — the existing test suite (`tests/test_memory.py`) must be updated and new tests added for the SQLite layer.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Single project**: `bot/` for source, `tests/` for tests at repository root

---

## Phase 1: Setup

**Purpose**: Create the new SQLite storage layer foundation

- [x] T001 Create SQLite storage module with schema creation, WAL mode config, and connection management in `bot/memory_store.py`
- [x] T002 [P] Add `bot/migrations/v0_21_0.py` migration stub (empty, will be filled in Phase 2)

**Checkpoint**: Storage layer module exists with schema DDL and connection helpers

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Implement `create_tables()` in `bot/memory_store.py`: create `entries` table (id, agent_name, tier, content, topic, tags, created_at, archived_at, archive_reason), `active_snapshots` table (agent_name PK, content, word_count, updated_at), and FTS5 virtual table on entries(content, topic, tags)
- [x] T004 Implement `get_connection(db_path)` in `bot/memory_store.py`: return sqlite3 connection with WAL mode, foreign keys, and row_factory configured. Handle first-run schema creation.
- [x] T005 [P] Implement `resolve_db_path(agent_name, agent_type, work_dir)` in `bot/memory_store.py`: map agent identity to `.db` file path following existing path conventions (orchestrator → `data/memory/robyx.db`, specialist → `data/memory/{name}.db`, workspace → `{work_dir}/.robyx/memory.db`)
- [x] T006 [P] Create `tests/test_memory_store.py` with tests for schema creation, WAL mode verification, connection management, and db path resolution

**Checkpoint**: Foundation ready — SQLite storage layer is functional and tested. User story implementation can now begin.

---

## Phase 3: User Story 1 — Workspace agent maintains project state across months (Priority: P1) MVP

**Goal**: Active memory load/save via SQLite with atomic writes, replacing file I/O. An agent's current-state context loads in <1ms from SQLite instead of reading a flat file.

**Independent Test**: Create a workspace agent, save active memory, reload it in a fresh connection, verify content matches. Simulate crash mid-write, verify no corruption.

### Tests for User Story 1

- [x] T007 [P] [US1] Test active memory round-trip (save → load → verify content) in `tests/test_memory_store.py`
- [x] T008 [P] [US1] Test active memory word budget enforcement (>5000 words triggers over-budget flag) in `tests/test_memory_store.py`
- [x] T009 [P] [US1] Test crash safety: write active memory, close connection uncleanly, reopen, verify data intact in `tests/test_memory_store.py`
- [x] T010 [P] [US1] Test `build_memory_context()` returns correct content from SQLite in `tests/test_memory.py`

### Implementation for User Story 1

- [x] T011 [US1] Implement `load_active_snapshot(conn, agent_name)` and `save_active_snapshot(conn, agent_name, content)` in `bot/memory_store.py`
- [x] T012 [US1] Refactor `load_active()` in `bot/memory.py` — kept as legacy file-based fallback; `build_memory_context()` now prefers SQLite
- [x] T013 [US1] Refactor `save_active()` in `bot/memory.py` — kept as legacy file-based fallback; SQLite path via `memory_store.save_active_snapshot()`
- [x] T014 [US1] Update `build_memory_context()` in `bot/memory.py` to try SQLite first, fall back to markdown
- [x] T015 [US1] Update `get_memory_instructions()` in `bot/memory.py` to reference the new search capability in the `MEMORY_INSTRUCTIONS` template
- [x] T016 [US1] Update all callers — no changes needed: `build_memory_context` and `get_memory_instructions` signatures unchanged

**Checkpoint**: Active memory works end-to-end via SQLite. Agents can save and load their current state atomically.

---

## Phase 4: User Story 2 — Historical context retrieval on demand (Priority: P2)

**Goal**: Archived entries are stored as individual indexed rows in SQLite with FTS5, enabling topic-based retrieval in <10ms on 10K entries.

**Independent Test**: Insert 500 archive entries across multiple topics, search by keyword, verify relevant results returned with BM25 ranking.

### Tests for User Story 2

- [x] T017 [P] [US2] Test `append_archive_entry()` stores entry with topic and tags in `tests/test_memory_store.py`
- [x] T018 [P] [US2] Test FTS5 search returns ranked results for keyword queries in `tests/test_memory_store.py`
- [x] T019 [P] [US2] Test FTS5 search with 1000 entries returns results in <100ms in `tests/test_memory_store.py`
- [x] T020 [P] [US2] Test search with no matches returns empty list (not false positives) in `tests/test_memory_store.py`

### Implementation for User Story 2

- [x] T021 [US2] Implement `append_archive_entry(conn, agent_name, content, reason, topic, tags)` in `bot/memory_store.py` — INSERT into entries + FTS5 trigger
- [x] T022 [US2] Implement `search_archive(conn, agent_name, query, limit=10)` in `bot/memory_store.py` — FTS5 MATCH with BM25 ranking, returns list of dicts
- [x] T023 [US2] Legacy `append_archive()` kept in `bot/memory.py` for backward compat; new SQLite path via `memory_store.append_archive_entry()`
- [x] T024 [US2] Add `search_memory()` function in `bot/memory.py` as new public API — delegates to `memory_store.search_archive()`
- [x] T025 [US2] Implement `list_archive_topics()` in `bot/memory_store.py` to query distinct topics from SQLite
- [x] T026 [US2] Update `build_memory_context()` archive note section to mention search capability instead of listing file names

**Checkpoint**: Archive entries are searchable. "What did we decide about X?" queries return relevant ranked results.

---

## Phase 5: User Story 3 — Memory survives context compaction (Priority: P2)

**Goal**: When the AI backend compacts context, the agent's memory is re-injected correctly on the next interaction. The consolidated project state from SQLite is available regardless of compaction events.

**Independent Test**: Verify `build_memory_context()` always returns the latest active snapshot, independent of any external state. Verify that saving updated memory after compaction persists correctly.

### Tests for User Story 3

- [x] T027 [P] [US3] Test that `build_memory_context()` output is deterministic (same input → same output, no stale caches) in `tests/test_memory.py`
- [x] T028 [P] [US3] Test save-then-load cycle simulating post-compaction update in `tests/test_memory.py`

### Implementation for User Story 3

- [x] T029 [US3] Verified `build_memory_context()` reads fresh from SQLite on every call (no in-memory caching). Added code comment documenting this is intentional.
- [x] T030 [US3] Updated `MEMORY_INSTRUCTIONS` in `bot/memory.py` to advise agents that memory persists across compaction events

**Checkpoint**: Memory is compaction-proof. Context reload always gets the latest state.

---

## Phase 6: User Story 4 — Multi-agent memory isolation (Priority: P3)

**Goal**: Each agent has its own SQLite database file. No cross-contamination. Robyx orchestrator can read (but not write) other agents' active snapshots for cross-project overview.

**Independent Test**: Create 3 agents with separate DBs, verify writes to one don't appear in another. Verify orchestrator aggregation reads from all.

### Tests for User Story 4

- [x] T031 [P] [US4] Test that two agents with different names get different `.db` file paths in `tests/test_memory_store.py`
- [x] T032 [P] [US4] Test that writing to agent A's DB does not affect agent B's DB in `tests/test_memory_store.py`
- [x] T033 [P] [US4] Test orchestrator can read active snapshots from multiple agent DBs in `tests/test_memory_store.py`

### Implementation for User Story 4

- [x] T034 [US4] Implement `aggregate_active_summaries(db_paths)` in `bot/memory_store.py` — reads active snapshot from each agent's DB, returns dict of `{agent_name: content}`
- [x] T035 [US4] Verified `get_memory_dir()` and `resolve_db_path()` produce non-overlapping paths for agents with different names/types

**Checkpoint**: All agents have isolated memory. Orchestrator can aggregate for cross-project views.

---

## Phase 7: Migration

**Purpose**: Automatic migration from markdown files to SQLite for existing installations

- [x] T036 Implement `migrate_markdown_to_sqlite(db_path, agent_name, memory_dir)` in `bot/memory_store.py` — parse `active.md` → INSERT into active_snapshots, parse `archive/YYYY-QN.md` → split by `---` separator → INSERT each entry into entries table, rename old files to `.md.bak`
- [x] T037 Wire migration into `bot/migrations/v0_21_0.py` — call `migrate_markdown_to_sqlite()` for all known agents (orchestrator, registered specialists, registered workspaces)
- [x] T038 [P] Test migration from markdown to SQLite with sample `active.md` and `archive/` files in `tests/test_memory_store.py`
- [x] T039 [P] Test migration idempotency: running twice produces same result in `tests/test_memory_store.py`
- [x] T040 [P] Test migration handles missing files gracefully (no `active.md`, empty `archive/`) in `tests/test_memory_store.py`

**Checkpoint**: Existing installations auto-migrate on first boot after update. No manual intervention needed.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [x] T041 [P] Update `tests/test_memory.py` — adapted tests that referenced old `active.md` string in instructions
- [x] T042 [P] Run full test suite (`pytest tests/`) — 1089 passed, 0 failed, 1 skipped
- [x] T043 Verified `has_native_claude_memory()` still works correctly — 6 dedicated tests pass
- [x] T044 [P] All new and modified public functions in `bot/memory.py` and `bot/memory_store.py` have docstrings
- [x] T045 Run quickstart.md verification scenarios manually (active memory round-trip, archive search, migration, crash safety)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 completion — BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Phase 2 — MVP target
- **User Story 2 (Phase 4)**: Depends on Phase 2 — can run in parallel with US1 (different functions)
- **User Story 3 (Phase 5)**: Depends on US1 completion (needs working active memory)
- **User Story 4 (Phase 6)**: Depends on Phase 2 — can run in parallel with US1/US2
- **Migration (Phase 7)**: Depends on US1 + US2 (needs both active and archive in SQLite)
- **Polish (Phase 8)**: Depends on all phases complete

### User Story Dependencies

- **User Story 1 (P1)**: Depends on Foundational only — no cross-story deps
- **User Story 2 (P2)**: Depends on Foundational only — can parallelize with US1
- **User Story 3 (P2)**: Depends on US1 (needs `build_memory_context()` working with SQLite)
- **User Story 4 (P3)**: Depends on Foundational only — can parallelize with US1/US2

### Within Each User Story

- Tests written first, verify they fail before implementation
- Store-layer functions before memory.py refactors
- Internal implementation before caller updates
- Story complete before moving to next priority

### Parallel Opportunities

- T001 + T002: Setup tasks on different files
- T005 + T006: Path resolution + its tests
- T007–T010: All US1 tests (different test functions)
- T017–T020: All US2 tests
- T027–T028: All US3 tests
- T031–T033: All US4 tests
- T038–T040: All migration tests
- US1 + US2 + US4: Can proceed in parallel after Phase 2

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (active memory via SQLite)
4. **STOP and VALIDATE**: Test active memory round-trip, crash safety
5. This alone is a shippable improvement (atomic writes, crash safety)

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. User Story 1 → Active memory works → **MVP!**
3. User Story 2 → Archive search works → Major value add
4. User Story 3 → Compaction-proof → Robustness
5. User Story 4 → Isolation verified → Multi-agent confidence
6. Migration → Existing users auto-upgraded
7. Polish → Full test suite green, docs updated

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Verify tests fail before implementing
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- The spec does NOT require TDD, but tests are included because the existing test suite (960+ tests) must be updated and the Constitution (Principle IV) mandates comprehensive testing
