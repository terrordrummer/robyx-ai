# Tasks: Full Code Review & Hardening

**Input**: Design documents from `specs/002-full-code-review/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Tests**: Bug fixes MUST include a corresponding test (FR-004 from spec).

**Organization**: Tasks are grouped by user story. Within each story, modules are ordered by the review groups defined in plan.md (A-F, by risk priority). Each task is a self-contained module review + fix pass.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1=bugs, US2=consistency, US3=security, US4=performance)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Establish review baseline

- [ ] T001 Run full test suite and record baseline: test count, pass count, and total LOC via `pytest tests/ -q` and `find bot -name '*.py' | xargs wc -l`
- [ ] T002 [P] Create findings report template at `specs/002-full-code-review/findings.md` with columns: ID, Module, Category, Severity, Description, Fix

**Checkpoint**: Baseline recorded, findings template ready

---

## Phase 2: User Story 1 — No latent bugs (Priority: P1)

**Goal**: Review every module for logic errors, unhandled exceptions, race conditions, and resource leaks. Fix all findings in-place. Add tests for each fix.

**Independent Test**: Full test suite passes with new tests covering each bug fix.

### Group A: Core / High Risk

- [ ] T003 [US1] Review and fix bugs in `bot/handlers.py` — focus on input parsing, command dispatch edge cases, unhandled exceptions in message processing
- [ ] T004 [US1] Review and fix bugs in `bot/scheduler.py` — focus on timer precision, concurrent task claims, crash recovery, late-fire logic
- [ ] T005 [US1] Review and fix bugs in `bot/ai_invoke.py` — focus on subprocess lifecycle, timeout handling, output parsing, error propagation
- [ ] T006 [US1] Review and fix bugs in `bot/bot.py` — focus on startup/shutdown ordering, signal handling, service lifecycle, PID lock
- [ ] T007 [US1] Review and fix bugs in `bot/updater.py` — focus on snapshot/rollback atomicity, smoke test, version comparison, file operations

### Group B: Platform Adapters

- [ ] T008 [P] [US1] Review and fix bugs in `bot/messaging/telegram.py` — focus on rate limiting, message splitting, error recovery
- [ ] T009 [P] [US1] Review and fix bugs in `bot/messaging/discord.py` — focus on thread management, permissions, reconnection
- [ ] T010 [P] [US1] Review and fix bugs in `bot/messaging/slack.py` — focus on socket mode, event dedup, error recovery
- [ ] T011 [P] [US1] Review and fix bugs in `bot/messaging/base.py` — focus on ABC completeness, PlatformMessage contract

### Group C: Agent & Task Management

- [ ] T012 [P] [US1] Review and fix bugs in `bot/agents.py` — focus on session state, concurrent access, agent creation/deletion
- [ ] T013 [P] [US1] Review and fix bugs in `bot/continuous.py` — focus on state file I/O, interruption handling, step execution
- [ ] T014 [P] [US1] Review and fix bugs in `bot/task_runtime.py` — focus on context resolution, missing agent handling
- [ ] T015 [P] [US1] Review and fix bugs in `bot/scheduled_delivery.py` — focus on output routing, silent delivery, error propagation
- [ ] T016 [P] [US1] Review and fix bugs in `bot/topics.py` — focus on topic creation, naming collisions, platform-specific behavior
- [ ] T017 [P] [US1] Review and fix bugs in `bot/collaborative.py` — focus on auth routing, workspace sharing, edge cases

### Group D: Configuration & Support

- [ ] T018 [P] [US1] Review and fix bugs in `bot/ai_backend.py` — focus on backend detection, model resolution, path handling
- [ ] T019 [P] [US1] Review and fix bugs in `bot/config.py` — focus on env var parsing, type coercion, missing values
- [ ] T020 [P] [US1] Review and fix bugs in `bot/config_updates.py` — focus on .env file mutation, concurrent writes
- [ ] T021 [P] [US1] Review and fix bugs in `bot/memory.py` — focus on SQLite fallback, legacy compat paths
- [ ] T022 [P] [US1] Review and fix bugs in `bot/memory_store.py` — focus on connection management, error handling
- [ ] T023 [P] [US1] Review and fix bugs in `bot/model_preferences.py` — focus on YAML parsing, missing keys
- [ ] T024 [P] [US1] Review and fix bugs in `bot/media.py` — focus on image compression, file handle leaks
- [ ] T025 [P] [US1] Review and fix bugs in `bot/voice.py` — focus on API call error handling

### Group E: Infrastructure

- [ ] T026 [P] [US1] Review and fix bugs in `bot/_bootstrap.py` — focus on import safety, dependency detection
- [ ] T027 [P] [US1] Review and fix bugs in `bot/process.py` — focus on subprocess lifecycle, signal propagation, zombie processes
- [ ] T028 [P] [US1] Review and fix bugs in `bot/authorization.py` — focus on permission checks, bypass risks
- [ ] T029 [P] [US1] Review and fix bugs in `bot/i18n.py` — focus on string formatting, missing keys, format specifier safety
- [ ] T030 [P] [US1] Review and fix bugs in `bot/session_lifecycle.py` — focus on invalidation logic, timing
- [ ] T031 [P] [US1] Review and fix bugs in `bot/orphan_tracker.py` — focus on orphan detection, cleanup, file I/O

### Group F: Migration Framework

- [ ] T032 [P] [US1] Review and fix bugs in `bot/migrations/runner.py` — focus on chain ordering, error recovery
- [ ] T033 [P] [US1] Review and fix bugs in `bot/migrations/tracker.py` — focus on JSON persistence, corruption handling
- [ ] T034 [P] [US1] Review and fix bugs in `bot/migrations/legacy.py` — focus on legacy compat, edge cases
- [ ] T035 [US1] Run full test suite after US1 fixes — verify zero regressions, all new tests pass

**Checkpoint**: All modules reviewed for bugs. Every fix has a test. Full suite green.

---

## Phase 3: User Story 2 — Consistent patterns (Priority: P2)

**Goal**: Normalize naming, logging, error handling patterns, and remove dead code across all modules.

**Independent Test**: Style audit shows uniform conventions; LOC count is lower than baseline.

- [ ] T036 [US2] Audit and remove dead code, unused imports, and unreachable branches across all `bot/*.py` files
- [ ] T037 [P] [US2] Normalize logging patterns across all modules — consistent logger names, log levels, message format
- [ ] T038 [P] [US2] Normalize error handling patterns — consistent exception hierarchy, no bare `except:`, no swallowed errors
- [ ] T039 [P] [US2] Normalize naming conventions — consistent function/variable naming across modules
- [ ] T040 [US2] Run full test suite after US2 changes — verify zero regressions

**Checkpoint**: Codebase reads as if written by one person. Dead code removed.

---

## Phase 4: User Story 3 — No security vulnerabilities (Priority: P2)

**Goal**: Audit all code that handles external input, tokens, credentials, or subprocess execution for security issues.

**Independent Test**: Security-focused review finds zero medium+ severity issues.

- [ ] T041 [US3] Security review of `bot/handlers.py` — command injection via user input, auth bypass, message content injection
- [ ] T042 [P] [US3] Security review of `bot/ai_invoke.py` — command injection in CLI invocation, output sanitization, env var leakage
- [ ] T043 [P] [US3] Security review of `bot/process.py` — subprocess argument injection, shell=True usage, env propagation
- [ ] T044 [P] [US3] Security review of `bot/config.py` and `bot/config_updates.py` — token exposure in logs, .env file permissions
- [ ] T045 [P] [US3] Security review of `bot/updater.py` — path traversal in update extraction, snapshot integrity
- [ ] T046 [P] [US3] Security review of `bot/topics.py` — topic name sanitization, platform-specific injection
- [ ] T047 [P] [US3] Security review of `bot/collaborative.py` and `bot/authorization.py` — auth checks, permission escalation
- [ ] T048 [P] [US3] Security review of `bot/memory_store.py` — SQL injection in FTS5 queries, path traversal in db_path resolution
- [ ] T049 [US3] Run full test suite after US3 fixes — verify zero regressions, security tests added

**Checkpoint**: Zero known security issues at medium severity or above.

---

## Phase 5: User Story 4 — Performance bottlenecks resolved (Priority: P3)

**Goal**: Identify and fix clear performance wins in hot paths: scheduler loop, message handling, AI invocation.

**Independent Test**: No blocking calls in async paths; no redundant I/O in hot loops.

- [ ] T050 [US4] Performance review of `bot/scheduler.py` — identify blocking I/O in the 60s tick, redundant file reads, O(n^2) patterns
- [ ] T051 [P] [US4] Performance review of `bot/handlers.py` — redundant lookups, unnecessary await chains, message processing latency
- [ ] T052 [P] [US4] Performance review of `bot/ai_invoke.py` — subprocess overhead, output buffering, timeout efficiency
- [ ] T053 [P] [US4] Performance review of `bot/bot.py` — startup time, service initialization, shutdown ordering
- [ ] T054 [US4] Run full test suite after US4 fixes — verify zero regressions

**Checkpoint**: Hot paths are clean. No blocking calls in async code.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification, findings report, and LOC comparison

- [ ] T055 Run full test suite — final green check across all changes
- [ ] T056 Compare final LOC with baseline from T001 — document reduction
- [ ] T057 Finalize `specs/002-full-code-review/findings.md` — complete findings report with all issues found and fixes applied
- [ ] T058 [P] Verify `has_native_claude_memory()` and platform adapters still work correctly after all changes

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **US1 Bugs (Phase 2)**: Depends on Setup — BLOCKS all other stories (bug fixes may change code that other stories review)
- **US2 Consistency (Phase 3)**: Depends on US1 (normalize after bugs are fixed, not before)
- **US3 Security (Phase 4)**: Depends on US1 (review the fixed code, not buggy code)
- **US4 Performance (Phase 5)**: Depends on US1 (optimize the fixed code)
- **Polish (Phase 6)**: Depends on all stories complete

### Within US1 (Phase 2)

- Group A modules (T003-T007) are sequential — they share handler/scheduler dependencies
- Group B modules (T008-T011) are parallel — each adapter is independent
- Group C modules (T012-T017) are parallel — each module is independent
- Group D modules (T018-T025) are parallel — each module is independent
- Group E modules (T026-T031) are parallel — each module is independent
- Group F modules (T032-T034) are parallel — each module is independent

### Parallel Opportunities

- All Group B-F module reviews within US1 are parallelizable (different files)
- US2, US3, US4 can partially overlap after US1 is complete (different concerns on same files)
- T036 (dead code removal) must happen before T037-T039 (pattern normalization)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (baseline)
2. Complete Phase 2: Bug review all modules
3. **STOP and VALIDATE**: Full test suite green, all bugs fixed with tests
4. This alone is the highest-value deliverable

### Incremental Delivery

1. Setup → Baseline recorded
2. US1 (bugs) → All bugs fixed → **MVP!**
3. US2 (consistency) → Dead code removed, patterns normalized
4. US3 (security) → Zero medium+ security issues
5. US4 (performance) → Hot paths optimized
6. Polish → Findings report complete, LOC comparison documented

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each module review task includes: read the full file, apply the review checklist from plan.md, fix all findings in-place, add tests for bug fixes
- Commit after each module or logical group of modules
- Stop at any checkpoint to validate independently
- FR-004: every bug fix MUST have a corresponding test
