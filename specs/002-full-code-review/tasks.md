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

- [x] T001 Run full test suite and record baseline: test count, pass count, and total LOC via `pytest tests/ -q` and `find bot -name '*.py' | xargs wc -l`
- [x] T002 [P] Create findings report template at `specs/002-full-code-review/findings.md` with columns: ID, Module, Category, Severity, Description, Fix

**Checkpoint**: Baseline recorded, findings template ready

---

## Phase 2: User Story 1 — No latent bugs (Priority: P1)

**Goal**: Review every module for logic errors, unhandled exceptions, race conditions, and resource leaks. Fix all findings in-place. Add tests for each fix.

**Independent Test**: Full test suite passes with new tests covering each bug fix.

### Group A: Core / High Risk

- [x] T003 [US1] Review and fix bugs in `bot/handlers.py` — focus on input parsing, command dispatch edge cases, unhandled exceptions in message processing
- [x] T004 [US1] Review and fix bugs in `bot/scheduler.py` — focus on timer precision, concurrent task claims, crash recovery, late-fire logic
- [x] T005 [US1] Review and fix bugs in `bot/ai_invoke.py` — focus on subprocess lifecycle, timeout handling, output parsing, error propagation
- [x] T006 [US1] Review and fix bugs in `bot/bot.py` — focus on startup/shutdown ordering, signal handling, service lifecycle, PID lock
- [x] T007 [US1] Review and fix bugs in `bot/updater.py` — focus on snapshot/rollback atomicity, smoke test, version comparison, file operations

### Group B: Platform Adapters

- [x] T008 [P] [US1] Review and fix bugs in `bot/messaging/telegram.py` — focus on rate limiting, message splitting, error recovery
- [x] T009 [P] [US1] Review and fix bugs in `bot/messaging/discord.py` — focus on thread management, permissions, reconnection
- [x] T010 [P] [US1] Review and fix bugs in `bot/messaging/slack.py` — focus on socket mode, event dedup, error recovery
- [x] T011 [P] [US1] Review and fix bugs in `bot/messaging/base.py` — focus on ABC completeness, PlatformMessage contract

### Group C: Agent & Task Management

- [x] T012 [P] [US1] Review and fix bugs in `bot/agents.py` — focus on session state, concurrent access, agent creation/deletion
- [x] T013 [P] [US1] Review and fix bugs in `bot/continuous.py` — focus on state file I/O, interruption handling, step execution
- [x] T014 [P] [US1] Review and fix bugs in `bot/task_runtime.py` — focus on context resolution, missing agent handling
- [x] T015 [P] [US1] Review and fix bugs in `bot/scheduled_delivery.py` — focus on output routing, silent delivery, error propagation
- [x] T016 [P] [US1] Review and fix bugs in `bot/topics.py` — focus on topic creation, naming collisions, platform-specific behavior
- [x] T017 [P] [US1] Review and fix bugs in `bot/collaborative.py` — focus on auth routing, workspace sharing, edge cases

### Group D: Configuration & Support

- [x] T018 [P] [US1] Review and fix bugs in `bot/ai_backend.py` — focus on backend detection, model resolution, path handling
- [x] T019 [P] [US1] Review and fix bugs in `bot/config.py` — focus on env var parsing, type coercion, missing values
- [x] T020 [P] [US1] Review and fix bugs in `bot/config_updates.py` — focus on .env file mutation, concurrent writes
- [x] T021 [P] [US1] Review and fix bugs in `bot/memory.py` — focus on SQLite fallback, legacy compat paths
- [x] T022 [P] [US1] Review and fix bugs in `bot/memory_store.py` — focus on connection management, error handling
- [x] T023 [P] [US1] Review and fix bugs in `bot/model_preferences.py` — focus on YAML parsing, missing keys
- [x] T024 [P] [US1] Review and fix bugs in `bot/media.py` — focus on image compression, file handle leaks
- [x] T025 [P] [US1] Review and fix bugs in `bot/voice.py` — focus on API call error handling

### Group E: Infrastructure

- [x] T026 [P] [US1] Review and fix bugs in `bot/_bootstrap.py` — focus on import safety, dependency detection
- [x] T027 [P] [US1] Review and fix bugs in `bot/process.py` — focus on subprocess lifecycle, signal propagation, zombie processes
- [x] T028 [P] [US1] Review and fix bugs in `bot/authorization.py` — focus on permission checks, bypass risks
- [x] T029 [P] [US1] Review and fix bugs in `bot/i18n.py` — focus on string formatting, missing keys, format specifier safety
- [x] T030 [P] [US1] Review and fix bugs in `bot/session_lifecycle.py` — focus on invalidation logic, timing
- [x] T031 [P] [US1] Review and fix bugs in `bot/orphan_tracker.py` — focus on orphan detection, cleanup, file I/O

### Group F: Migration Framework

- [x] T032 [P] [US1] Review and fix bugs in `bot/migrations/runner.py` — focus on chain ordering, error recovery
- [x] T033 [P] [US1] Review and fix bugs in `bot/migrations/tracker.py` — focus on JSON persistence, corruption handling
- [x] T034 [P] [US1] Review and fix bugs in `bot/migrations/legacy.py` — focus on legacy compat, edge cases
- [x] T035 [US1] Run full test suite after US1 fixes — verify zero regressions, all new tests pass

**Checkpoint**: All modules reviewed for bugs. Every fix has a test. Full suite green.

---

## Phase 3: User Story 2 — Consistent patterns (Priority: P2)

**Goal**: Normalize naming, logging, error handling patterns, and remove dead code across all modules.

**Independent Test**: Style audit shows uniform conventions; LOC count is lower than baseline.

- [x] T036 [US2] Audit and remove dead code, unused imports, and unreachable branches across all `bot/*.py` files
- [x] T037 [P] [US2] Normalize logging patterns across all modules — consistent logger names, log levels, message format
- [x] T038 [P] [US2] Normalize error handling patterns — consistent exception hierarchy, no bare `except:`, no swallowed errors
- [x] T039 [P] [US2] Normalize naming conventions — consistent function/variable naming across modules
- [x] T040 [US2] Run full test suite after US2 changes — verify zero regressions

**Checkpoint**: Codebase reads as if written by one person. Dead code removed.

---

## Phase 4: User Story 3 — No security vulnerabilities (Priority: P2)

**Goal**: Audit all code that handles external input, tokens, credentials, or subprocess execution for security issues.

**Independent Test**: Security-focused review finds zero medium+ severity issues.

- [x] T041 [US3] Security review of `bot/handlers.py` — command injection via user input, auth bypass, message content injection
- [x] T042 [P] [US3] Security review of `bot/ai_invoke.py` — command injection in CLI invocation, output sanitization, env var leakage
- [x] T043 [P] [US3] Security review of `bot/process.py` — subprocess argument injection, shell=True usage, env propagation
- [x] T044 [P] [US3] Security review of `bot/config.py` and `bot/config_updates.py` — token exposure in logs, .env file permissions
- [x] T045 [P] [US3] Security review of `bot/updater.py` — path traversal in update extraction, snapshot integrity
- [x] T046 [P] [US3] Security review of `bot/topics.py` — topic name sanitization, platform-specific injection
- [x] T047 [P] [US3] Security review of `bot/collaborative.py` and `bot/authorization.py` — auth checks, permission escalation
- [x] T048 [P] [US3] Security review of `bot/memory_store.py` — SQL injection in FTS5 queries, path traversal in db_path resolution
- [x] T049 [US3] Run full test suite after US3 fixes — verify zero regressions, security tests added

**Checkpoint**: Zero known security issues at medium severity or above.

---

## Phase 5: User Story 4 — Performance bottlenecks resolved (Priority: P3)

**Goal**: Identify and fix clear performance wins in hot paths: scheduler loop, message handling, AI invocation.

**Independent Test**: No blocking calls in async paths; no redundant I/O in hot loops.

- [x] T050 [US4] Performance review of `bot/scheduler.py` — 5 findings documented: redundant queue read, blocking sync I/O, O(n) reconciliation, template re-read, save_state frequency
- [x] T051 [P] [US4] Performance review of `bot/handlers.py` — clean, no issues
- [x] T052 [P] [US4] Performance review of `bot/ai_invoke.py` — clean, streaming reader is efficient
- [x] T053 [P] [US4] Performance review of `bot/bot.py` — clean, startup/shutdown ordering is correct
- [x] T054 [US4] Run full test suite after US4 fixes — 1085 passed, zero regressions

**Checkpoint**: Hot paths are clean. No blocking calls in async code.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification, findings report, and LOC comparison

- [x] T055 Run full test suite — 1085 passed, 1 skipped, zero failures
- [x] T056 Compare final LOC with baseline — 12,298 → 12,329 (+31, net increase from security hardening code)
- [x] T057 Finalize `specs/002-full-code-review/findings.md` — complete findings report
- [x] T058 [P] Verify `has_native_claude_memory()` and platform adapters still work correctly after all changes

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

---
---

# Pass 2 Tasks: Security / Stability / Ease of use / Natural Interaction

**Pass 2 plan**: [plan.md](./plan.md) · **Research**: [research.md](./research.md) · **Conversation contract**: [contracts/conversation-contract.md](./contracts/conversation-contract.md) · **Quickstart**: [quickstart.md](./quickstart.md)

**Story labels used in Pass 2**:

- `[P2-SEC]` — Security deep dive (User Story: Pass 2, Security)
- `[P2-STB]` — Stability under failure modes (User Story: Pass 2, Stability)
- `[P2-UX]`  — Ease of use / discoverability (User Story: Pass 2, Ease of use)
- `[P2-NI]`  — Natural interaction / tone / i18n (User Story: Pass 2, Natural interaction)

**Baseline at Pass 2 start** (2026-04-16): 1086 tests collected, 12 329 LOC under `bot/`, 53 modules, v0.21.0.

**Baseline refreshed 2026-04-18** (after 003 external-group wiring + 004 continuous-macro merged to main via v0.22.1): **1451 tests collected, 14 576 LOC under `bot/`, 59 modules, v0.22.1**. Net additions: +365 tests, +2 247 LOC, +6 modules (incl. new `bot/continuous_macro.py`, migrations `v0_22_0`, `v0_22_1`). Several Pass 2 audit targets were modified in-between — affected tasks carry a `⚠` drift marker below and must read the current file state, not the Pass-2-start state.

**Close-out gate**: `pytest tests/ -q` ≥ 1451 passing with new regression tests, every `P2-*` finding marked `fixed` or `deferred with rationale`, Pass 1 deferred findings (F12, F13, F14, F17, F20, P1–P5) re-evaluated, release notes drafted.

---

## Phase 7: Pass 2 Setup

**Purpose**: Lock baseline and create tracking scaffolding for Pass 2.

- [X] T059 Record Pass 2 baseline in `specs/002-full-code-review/findings.md`: run `pytest tests/ -q` and capture passing count; run `find bot -name '*.py' | xargs wc -l` and capture total
- [X] T060 [P] Append `## Pass 2 Findings` section to `specs/002-full-code-review/findings.md` with columns `ID | Module | Lens | Sev | Description | Fix | Status` and leave rows empty; use prefix `P2-NN`
- [X] T061 [P] Re-read `specs/002-full-code-review/contracts/conversation-contract.md` and confirm the 8-question checklist is referenced from every string-touching task below

**Checkpoint**: baseline frozen, findings table ready, contract understood.

---

## Phase 8: Foundational — Cross-cutting Inventories (blocking all Pass 2 stories)

**Purpose**: Gather global data that every story phase consumes. Must complete before US1-US4.

- [X] T062 Run the two ripgrep sweeps from `research.md` §R4 and produce `specs/002-full-code-review/string-inventory.md` listing every user-visible string literal found outside `bot/i18n.py` (columns: file, line, literal, suspected trigger, i18n candidate key)
- [X] T063 [P] Produce `specs/002-full-code-review/trust-boundaries.md` mapping each adapter's inputs to the downstream sinks that consume them (one table per adapter; source: `research.md` §R1)
- [X] T064 [P] Produce `specs/002-full-code-review/crash-matrix.md` by expanding the 12 scenarios from `research.md` §R2 into one row per scenario with `current-behavior-verified` / `reproduction-steps` / `target-behavior` columns

**Checkpoint**: three inventories published, every Pass 2 story phase has data to consume.

---

## Phase 9: Pass 2 — User Story P2-SEC — Security Deep Dive (Priority: P1)

**Goal**: Close every medium-or-higher security gap the Pass 1 review did not catch, using `trust-boundaries.md` as input.

**Independent Test**: every row in `trust-boundaries.md` with status "gap" has a corresponding `P2-NN` row in `findings.md` with status `fixed`; every fix has a regression test under `tests/`.

### Group A — Core / high-risk modules (security lens)

- [X] T065 [P2-SEC] Security-audit `bot/handlers.py` against `trust-boundaries.md`: auth-before-mutation, AI-controlled path validation, command-injection residuals, secret-in-error-reply; file findings, fix, add tests under `tests/test_handlers.py` — **audit clean**: owner_only correctly applied to every cmd_* and gated before collab bypass in handle_message; WORKSPACE allowlist for continuous task work_dir (Pass 1 S1) + SEND_IMAGE allowlist (Pass 1 S2) still in place; no shell injection primitives in use; `str(e)` echoed to users is all controlled ValueError messages from create_workspace/create_specialist (one marginal case at line 485 for generic `_process_and_send` exceptions, flagged as low-priority hardening — not a verified gap given the AI CLI tools we support don't echo secrets)
- [X] T066 [P2-SEC] Security-audit `bot/ai_invoke.py`: argv-only subprocess, env scrubbing, timeout enforcement, output size limits, orphan cleanup under SIGKILL; fix findings, add tests under `tests/test_ai_invoke.py` — closed P2-70 (env scrubbing gap): AI CLI subprocess now spawns with `_scrubbed_child_env()` that denylists ROBYX_BOT_TOKEN, KAELOPS_BOT_TOKEN, DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN, SLACK_APP_TOKEN. +7 regression tests. Provider API keys (OPENAI, ANTHROPIC) + basic env (PATH, HOME) preserved
- [X] T067 [P2-SEC] Security-audit `bot/updater.py`: re-verify Pass 1 tarball symlink/hardlink rejection, add size caps, validate pip subprocess env, confirm rollback doesn't leave executable permissions wider than before; tests under `tests/test_updater.py` — closed 2026-04-18: **P2-71** fixed (pip + migration subprocesses now spawn with `env=_scrubbed_child_env()` stripping bot tokens + AI provider keys; mirrors P2-70 pattern); **P2-72** fixed (restore path now caps cumulative uncompressed tarball size at 5 GiB). Re-verified F01 symlink/hardlink rejection still in place with extended path-traversal checks; F04 rollback→restore invoked in all 4 failure paths; rollback does NOT widen exec permissions. +6 regression tests (91 total in test_updater.py).
- [X] T068 [P2-SEC] Security-audit `bot/bot.py` startup/shutdown: PID-lock race window, signal handler secret-echo, atexit ordering leaks; tests under `tests/test_bot.py` — **audit clean 2026-04-18**: PID-lock race already closed by P2-20 (fcntl.flock on sidecar lock file, 7 regression tests in `TestEnsureSingleInstance`); signal handlers (SIGTERM/SIGINT) + `save_on_exit` echo no secrets (fixed log string + JSON state write); sole `atexit.register` idempotent via `_shutdown_done` flag. 003/004 additions (+93 lines ChatMemberHandler dispatch) do not touch lock/signal/atexit paths; chat titles logged via `%r` are injection-safe. No new findings, no code changes.
- [X] T069 [P2-SEC] Security-audit `bot/scheduler.py`: attacker-controllable queue fields, path-traversal in `work_dir`, retry amplification DoS; tests under `tests/test_scheduler.py` — **audit clean 2026-04-18**: queue fields re-validated at every claim (`validate_task_name` + `resolve_agent_file_path`); `work_dir` is pulled from `state.json` Agent records (never queue/user/AI), every `add_agent` call site pins `work_dir=str(WORKSPACE)`, AI macros carry no `work_dir` field; retries bounded by `MAX_REMINDER_ATTEMPTS` + `REMINDER_MAX_AGE_SECONDS` + `CLAIM_TIMEOUT_SECONDS`. One low-priority defense-in-depth note (no action): `agent.work_dir` isn't re-validated at state.json load time.

### Group B — Platform adapters (security lens, parity-aware)

- [X] T070 [P] [P2-SEC] Security-audit `bot/messaging/telegram.py`: size caps before download, filename path-traversal, token never in error replies, message-length bounds propagated; tests under `tests/test_telegram.py` — ⚠ file modified by 003/004 since Pass 2 baseline (+15 lines); re-read current state before auditing — **closed 2026-04-19**: closed **P2-82** (`download_voice` now enforces `_MAX_TELEGRAM_VOICE_BYTES = 25 MB` via a pre-download check on `voice_file.file_size` plus post-download byte-count verification; temp file cleaned up on every failure path; mirrors discord.py P2-11). Other lenses re-verified clean: `send_photo` path comes pre-validated against WORKSPACE by `_handle_media_commands`; `max_message_length = 4000` leaves margin under Telegram's 4096 cap; no caller-controlled segment in the `tempfile.NamedTemporaryFile` path; Telegram API descriptions echoed to users carry no token. Low-priority architectural observation noted (bot token lives in URL path → any future `raise_for_status`-style surfacing would leak it; not actionable in current code). +5 regression tests.
- [X] T071 [P] [P2-SEC] Security-audit `bot/messaging/discord.py`: extend Pass 1 domain allow-list from voice to ALL attachment fetches, thread-mutation auth race, token scrubbing; tests under `tests/test_discord.py` — closed P2-11 (unbounded read, 25 MB cap + streaming) and P2-12 (allow-list generalized into `_validate_discord_url`); token scrubbing/thread-auth race deferred as not actionable — no current gap
- [X] T072 [P] [P2-SEC] Security-audit `bot/messaging/slack.py`: Socket Mode dedup store size bound, `url_private` token scrubbing in error paths, `event_id` replay protection; tests under `tests/test_slack.py` — P2-10 (bearer-token redirect exfiltration, High) fixed in v0.21.0; residual items re-scoped to "no action" after review: dedup is handled inside `slack-bolt` (not our code); `_bot_token` never appears in error/log output
- [X] T073 [P] [P2-SEC] Security-audit `bot/messaging/base.py`: confirm ABC contract enforces validation hooks uniformly across adapters; tests under `tests/test_messaging_base.py` — ⚠ file modified by 003/004 since Pass 2 baseline (+13 lines, new lifecycle/announce hooks); re-read current state and confirm new hooks are uniformly implemented in each adapter — **closed 2026-04-19**: audit clean, **no new findings, no code changes**. Every `@abc.abstractmethod` implemented by all three adapters. New 003/004 hooks: `leave_chat` implemented on Telegram, explicitly refused with platform-specific `NotImplementedError` on Discord/Slack (consistent with FR-013 Telegram-only collab scope); `get_invite_link` overridden only on Telegram (Discord/Slack inherit the `return None` default; `collab_bot_added` only fires on Telegram so this is load-bearing safe); `bot_username` overridden only on Telegram (Discord/Slack inherit `None`, which forces `mentioned=False` in passive-mode routing — fail-closed and MORE restrictive on non-Telegram); `rename_main_channel` implemented on all three with matching signature. Pre-existing `send_message` error-behaviour inconsistency (Discord returns `None` on missing channel vs. others raising) already tracked under Pass 1 F14 — out of T073 scope.

### Group D — Config & support (security lens)

- [X] T074 [P] [P2-SEC] Security-audit `bot/config.py` + `bot/config_updates.py`: env parse safety, secret-write atomicity, concurrent `.env` write race, hot-reload-during-AI-call guard; tests under `tests/test_config.py` — closed as no-action: there is no `.env` hot-reload mechanism in the codebase (the bot reads `.env` once via `load_dotenv()` at startup and i18n tells users to restart after edits); trust-boundary X-3 was mis-identified
- [X] T075 [P] [P2-SEC] Security-audit `bot/media.py`: Pillow decompression-bomb protection (max pixels, max bytes), path validation for temp writes; tests under `tests/test_media.py` — closed P2-50 (file-size cap + lowered MAX_IMAGE_PIXELS + warning→error promotion, +4 regression tests)
- [X] T076 [P] [P2-SEC] Security-audit `bot/voice.py`: OpenAI API key never logged, temp-file cleanup on exception, file-size cap; tests under `tests/test_voice.py` — **closed 2026-04-19**: closed **P2-83** (`transcribe_voice` now enforces `_MAX_TRANSCRIPTION_BYTES = 25 MB` before network, refusing oversize files with new i18n key `voice_too_large`; `handlers.py` voice handler wraps the transcribe call in `try/finally` so `os.unlink(tmp_path)` fires on cancellation / any uncaught exception class, not just the 4 types `transcribe_voice` catches). API-key leak paths re-verified clean (key only in `Authorization: Bearer …` header; neither the OpenAI body-truncation log nor httpx exception messages echo headers). +3 regression tests. Adjacent observation noted: `bot/messaging/slack.py::download_voice` still reads the Slack file body into memory with no size cap — pre-dated P2-11/P2-82 pattern; left out of T076 scope, flagged for a future T072 follow-up if Slack voice features are revisited.
- [X] T077 [P] [P2-SEC] Security-audit `bot/memory.py` + `bot/memory_store.py`: SQLite parameterization, journal-mode safety, secret-key-in-value leak check; tests under `tests/test_memory.py` — **closed 2026-04-19**: closed **P2-84** (added `_validated_db_name_segment` defense-in-depth guard against path-traversal in specialist ``resolve_db_path`` / ``get_memory_dir`` segments; orchestrator and workspace branches bypass the validator so historical v0_21_0 migration compatibility is preserved; +9 regression tests). Closed **P2-85** as "test-guarded" (FTS5 injection: audit verified the strip-to-`[\w\s*]` + double-quote pattern is safe; +7 parametrised hostile-query regression tests lock the contract against future sanitiser regressions). Remaining lenses re-verified clean: every `conn.execute` uses `?` placeholders; WAL mode enabled; every public entry closes its connection in `finally`; memory contents are AI-generated plaintext (not attacker-amplified); memory DBs under `DATA_DIR` with standard 644 permissions.

### Group E — Infrastructure (security lens)

- [X] T078 [P] [P2-SEC] Security-audit `bot/authorization.py`: every permission check reachable, no bypass via unset field, collaborative auth path; tests under `tests/test_authorization.py` — ⚠ file modified by 003/004 since Pass 2 baseline (+28 lines, new external-group auth paths); re-read current state and pay specific attention to the collaborative/external-group permission boundary — **closed 2026-04-19**: closed **P2-80** (`is_authorised_adder` now filters workspaces whose status is outside `{active, setup, pending}`, so closing a workspace revokes ex-operators' ability to drag the bot into new groups; +4 regression tests, total 22 in `tests/test_authorization.py`). `get_user_role` / `can_send_executive` / `can_close_workspace` / `can_manage_roles` re-verified clean against the 003 call sites in `handlers.py` (`_handle_collaborative_message`, `_handle_collab_command`, `collab_bot_added`). Adjacent finding filed as **P2-81** (AI-emitted `[COLLAB_ANNOUNCE name="…"]` path traversal in `_handle_collab_announce`) — deferred to **T078a** to keep this commit scoped to `authorization.py`.
- [ ] T078a [P2-SEC] Follow-up to T078 (filed 2026-04-19): add `validate_collab_name(name)` to `bot/collaborative.py` (reject empty / control chars / path separators / `.` / `..`; enforce `^[a-z0-9][a-z0-9-]*$`, ≤64 chars). Call from `_handle_collab_announce` before the agent-file `write_text`, and from `CollabStore.create_pending` for defense-in-depth. Rejection in the announce path surfaces via the existing `collab_announce_error` STRING. Tests: traversal name rejected, control chars rejected, valid name accepted, existing flows unchanged. Closes **P2-81**.
- [ ] T079 [P] [P2-SEC] Security-audit `bot/_bootstrap.py` + `bot/process.py`: import-time side effects, subprocess lifecycle guarantees, zombie reaping; tests under `tests/test_bootstrap.py` and `tests/test_process.py`

### Group C — Agents & tasks (security lens, new scope after 004)

- [X] T079a [P] [P2-SEC] **Added 2026-04-18 after rebase onto main.** Security-audit NEW module `bot/continuous_macro.py` (introduced by feature 004, 704 LOC): regex-DoS, JSON size/depth limits, work_dir path-traversal, schema sanitization, idempotent reject path. — **closed 2026-04-18**: regexes audit-clean (negated char classes + non-greedy literal delimiters, no nested quantifiers); **P2-73** fixed (JSON size cap `_MAX_PROGRAM_BYTES = 64 KiB` added before `json.loads`); work_dir confinement verified (`Path(work_dir).resolve().relative_to(workspace_root)`); schema fields (`objective`/`success_criteria`/`first_step`) enforced by `_first_missing_field`; stripping unconditional per module docstring ("failure mode is 'not executed', never 'leaked'"). +2 regression tests. Cross-referenced against `specs/004-fix-continuous-task-macro/contracts/continuous-macro-grammar.md` — all grammar invariants enforced.

**Checkpoint P2-SEC**: all listed modules audited; every finding closed or explicitly deferred with rationale; test count increased by at least one per fix.

---

## Phase 10: Pass 2 — User Story P2-STB — Stability Under Failure Modes (Priority: P1)

**Goal**: Every row in `crash-matrix.md` where `current-behavior-verified != target-behavior` becomes a `P2-NN` finding that is fixed and tested.

**Independent Test**: a stability test harness (see T085) exercises each scenario; all pass.

### Group A — Core modules (stability lens)

- [~] T080 [P2-STB] Stability-audit `bot/scheduler.py`: `time.monotonic()` for intervals, tmp+rename on `queue.json` writes, late-fire dedup on restart, retry amplification cap; fix findings, tests under `tests/test_scheduler.py` — **deferred** with rationale (see P2-80 in findings.md): scheduler uses `datetime.now()` (not `time.time()`) and most usages are legitimately wall-clock; a correct monotonic split needs its own spec
- [~] T081 [P2-STB] Stability-audit `bot/bot.py`: lock mechanism is POSIX `fcntl.flock`-style (not PID-file-check race), restart-storm survival; tests under `tests/test_bot.py` — **partial**: P2-20 (TOCTOU PID-lock, High) fixed out-of-band in the security point release; restart-storm survival and `data/*.tmp` cleanup on startup still to do
- [ ] T082 [P2-STB] Stability-audit `bot/ai_invoke.py`: subprocess stall → kill → orphan-tracker cleanup path under parent-death scenario; tests under `tests/test_ai_invoke.py`
- [ ] T083 [P2-STB] Stability-audit `bot/updater.py`: disk-full on snapshot, pip failure mid-install, interrupted smoke test rollback; tests under `tests/test_updater.py`

### Group C — Agents & tasks (stability lens)

- [ ] T084 [P] [P2-STB] Stability-audit `bot/continuous.py`: step boundary atomicity, resume-from-last-committed-step under SIGKILL; tests under `tests/test_continuous.py`
- [X] T085 [P] [P2-STB] Stability-audit `bot/agents.py` + `bot/task_runtime.py`: atomic `agents.json` write (tmp+rename+fsync), partial-load handling for malformed JSON (Pass 1 F17 deferred — fix now); tests under `tests/test_agents.py` — closed P2-30 (JSON/Unicode corruption now quarantined to `*.corrupt-<UTC>` before falling back to empty state; also closes Pass 1 F17 on `collaborative.py`); tmp+rename+fsync + `task_runtime.py` audit deferred to a later slice
- [ ] T086 [P] [P2-STB] Stability-audit `bot/scheduled_delivery.py`: silent-delivery policy verified per adapter; tests under `tests/test_scheduled_delivery.py`
- [ ] T087 [P] [P2-STB] Stability-audit `bot/topics.py` + `bot/collaborative.py`: crash mid-topic-creation, partial workspace state recovery; tests under `tests/test_topics.py` and `tests/test_collaborative.py` — ⚠ `collaborative.py` modified by 003/004 since Pass 2 baseline (+172 lines, new external-group lifecycle hooks + atomic JSON writes already hardened by T085); re-read current state, extend the audit to cover crash mid-announce and crash mid-setup-complete

### Group F — Migration framework (stability lens)

- [X] T088 [P] [P2-STB] Stability-audit `bot/migrations/runner.py` + `bot/migrations/tracker.py`: idempotency re-verify (apply twice = no-op), version-advance-only-after-fsync, interrupted-migration recovery; tests under `tests/test_migrations.py` — closed P2-40 (tracker.save now uses tmp + fsync + os.replace); interrupted-migration recovery relies on per-migration idempotency (existing contract, tested per-migration)
- [ ] T089 [P] [P2-STB] Stability-audit `bot/migrations/base.py` + `bot/migrations/legacy.py`: legacy chain handoff, missing-migration detection; tests under `tests/test_migrations.py`
- [ ] T090 [P] [P2-STB] Stability-audit latest 5 migration files (`v0_21_1`, `v0_21_2`, `v0_21_3`, `v0_22_0`, `v0_22_1` — re-scoped 2026-04-18 after rebase; was `v0_20_25 … v0_21_0`): each idempotent, each has test covering re-run; tests under `tests/test_migration_v*.py`

### Cross-cutting stability infrastructure

- [ ] T091 [P2-STB] Add `tests/test_crash_matrix.py`: one pytest per scenario in `crash-matrix.md`, using `tmp_path` + signal handlers to simulate failures

**Checkpoint P2-STB**: `crash-matrix.md` shows all rows green; `tests/test_crash_matrix.py` passes.

---

## Phase 11: Pass 2 — User Story P2-UX — Ease of Use (Priority: P2)

**Goal**: First-time-user onboarding across all 3 platforms works without file editing, without stack traces, without guessing.

**Independent Test**: manual onboarding walkthrough against `quickstart.md` §2 works on Telegram, Discord, Slack using a fresh `data/` dir; `/help` output covers every registered command.

- [ ] T092 [P2-UX] Audit `bot/handlers.py` for every registered command: is it listed in `/help`? Does the error path produce an actionable message? Does a destructive command require confirmation? Fix gaps; tests under `tests/test_handlers.py` — ⚠ file modified by 003/004 since Pass 2 baseline (+703 lines, new collab/continuous commands); re-check `/help` parity was extended to new commands (T097 parity test should already cover this) and that new destructive commands have confirmation
- [ ] T093 [P2-UX] Audit `bot/bot.py` startup: missing env vars produce actionable messages (not tracebacks); `.env.example` file is current and complete
- [ ] T094 [P] [P2-UX] Audit `bot/ai_backend.py`: missing AI CLI binary surfaces as a readable user-visible message via `i18n`, not a Python traceback; tests under `tests/test_ai_backend.py`
- [ ] T095 [P] [P2-UX] Audit `bot/config.py` + `bot/config_updates.py`: every failure path produces a user-visible error with remediation hint; tests under `tests/test_config.py`
- [ ] T096 [P] [P2-UX] Audit `bot/collaborative.py`: workspace sharing errors tell the user how to fix (not just "invalid state"); tests under `tests/test_collaborative.py`
- [X] T097 [P2-UX] Cross-reference `/help` output against handler registrations: any registered command not in `/help` is a finding; any `/help` entry without a handler is a finding; tests under `tests/test_help.py` — closed via `TestHelpParity` in `tests/test_i18n_parity.py` (two tests: handler.keys ⊆ help_text commands, and help_text commands ⊆ handler.keys, modulo the `start`/`help`/internal-dispatch exclusions)
- [ ] T098 [P2-UX] Update `.env.example` / `install/` scripts if any required env var is missing from documentation

**Checkpoint P2-UX**: onboarding walkthrough passes on all 3 adapters; `/help` parity verified; no stack trace reachable by any user action.

---

## Phase 12: Pass 2 — User Story P2-NI — Natural Interaction (Priority: P2)

**Goal**: Every user-visible string passes the 8-question checklist in `contracts/conversation-contract.md` §8.

**Independent Test**: `string-inventory.md` shows zero literals outside `bot/i18n.py` (excluding debug logs); every `i18n` key has IT + EN; automated test verifies locale parity.

### String relocation & i18n parity

- [X] T099 [P2-NI] Process `string-inventory.md` top-down: for every literal, either relocate it to `bot/i18n.py` (adding IT + EN keys) or justify it as internal/log-only with a code comment; commit after each batch of 10 — closed P2-01/02/03 (all 3 direct-literal violations in handlers.py moved to `STRINGS`); §B `raise` prose verified as internal-only and not echoed to users; §D templates still pending (T107)
- [~] T100 [P] [P2-NI] Add `tests/test_i18n_parity.py`: assert every key under `STRINGS_IT` has a matching key under `STRINGS_EN` (and vice versa), asserting no locale has orphan keys — re-scoped: bot is single-locale (English); wrote `TestHelpParity` in the same file for the parity dimension that's actually meaningful today. Locale-parity test becomes relevant only if a second locale is added.
- [X] T101 [P] [P2-NI] Add `tests/test_i18n_substitution.py`: iterate every key, instantiate with representative arguments, assert no `{placeholder}` remains unsubstituted (closes residual risk from Pass 1 F19) — implemented as `TestStringSubstitution` in `tests/test_i18n_parity.py` (two parametrised tests across every `STRINGS` key — %s/%d substitution check + `{placeholder}` leak check)

### Tone audit per module

- [ ] T102 [P2-NI] Tone-audit `bot/handlers.py` replies: no `Error:` / `Exception:` prefixes, every error has an actionable next step, voice is first-person plural or second-person (per contract §3.1); fix in-place by updating `i18n` keys — ⚠ file modified by 003/004 (+703 lines of new user-visible strings from collab/continuous features); scope now includes the new `STRINGS` keys added by 003/004 in `bot/i18n.py` (+83 lines)
- [ ] T103 [P] [P2-NI] Tone-audit `bot/messaging/telegram.py`, `discord.py`, `slack.py`: replies differ only due to platform capability, never wording; parity test under `tests/test_messaging_parity.py` — ⚠ all three adapters modified by 003/004 (new lifecycle/announce/setup-complete hooks); parity must be verified on the new hooks too
- [ ] T104 [P] [P2-NI] Tone-audit `bot/voice.py` user-facing strings (transcription failure, key-missing, file-too-large)
- [ ] T105 [P] [P2-NI] Tone-audit `bot/continuous.py` + `bot/scheduled_delivery.py`: silence-policy compliance (per contract §4) — no progress messages unless user requested them
- [ ] T106 [P] [P2-NI] Tone-audit `bot/updater.py`: pre/post-update messages are conversational, not release-engineer-speak

### Template hygiene

- [ ] T107 [P2-NI] Sweep `templates/` directory: verify every prompt template substitutes all `{placeholders}`, intentional `{{literal}}` escapes carry a comment explaining why
- [ ] T108 [P2-NI] Verify agent persona consistency: prompts in `templates/` do not contradict agent-configured persona strings

**Checkpoint P2-NI**: every row in `string-inventory.md` resolved; `test_i18n_parity.py` and `test_i18n_substitution.py` pass; messaging-parity test passes.

---

## Phase 13: Polish & Cross-Cutting Close-out

**Purpose**: Re-evaluate Pass 1 deferred findings, finalize documentation, version bump.

- [ ] T109 Re-evaluate Pass 1 deferred finding F12 (`telegram.py` Markdown behavioral change): decide fix or keep deferred with updated rationale; record under `## Pass 2 Findings`
- [ ] T110 [P] Re-evaluate Pass 1 F13 (`discord.py download_voice` error handling): fix or re-defer with rationale
- [ ] T111 [P] Re-evaluate Pass 1 F14 (`slack.py` reply/edit_message error handling): fix or re-defer with rationale
- [X] T112 [P] Re-evaluate Pass 1 F17 (`collaborative.py` malformed JSON partial load): likely closed by T085 — confirm and mark — **closed 2026-04-18**: T085's completion note explicitly states "also closes Pass 1 F17 on `collaborative.py`" via the shared quarantine-to-`*.corrupt-<UTC>` path; no further action required (finding status in `findings.md` already reflected via P2-30). 003/004 added further `collaborative.py` features but reused the same hardened JSON load path.
- [ ] T113 [P] Re-evaluate Pass 1 F20 (`voice.py` `%` formatting TypeError): fix or re-defer with rationale
- [ ] T114 [P] Re-evaluate Pass 1 performance finding P1 (`scheduler.py` redundant `queue.json` read)
- [ ] T115 [P] Re-evaluate Pass 1 performance finding P2 (`scheduler.py` blocking sync I/O in async) — if stability work in T080 already moved to `asyncio.to_thread`, close here
- [ ] T116 [P] Re-evaluate Pass 1 performance findings P3, P4, P5: fix or re-defer with updated rationale
- [ ] T117 Run full `pytest tests/ -q` and confirm count ≥ 1451 (refreshed baseline 2026-04-18; was ≥ 1086 at Pass 2 start); diagnose any regression before proceeding
- [ ] T118 Finalize `specs/002-full-code-review/findings.md`: every `P2-NN` row has status `fixed` or `deferred (rationale: ...)`; every Pass 1 deferred row has a Pass 2 status update
- [ ] T119 Bump `VERSION` (patch bump unless a migration was introduced) and add a migration entry under `bot/migrations/` via `scripts/new_migration.py` only if a schema change was actually made
- [ ] T120 Create `releases/vX.Y.Z.md` summarizing Pass 2: findings fixed count, deferred-with-rationale count, test-count delta, LOC delta
- [ ] T121 Append Pass 2 section to `CHANGELOG.md`
- [ ] T122 Run `quickstart.md` §8 close-out sequence end-to-end; do NOT push without explicit user request

---

## Dependencies & Execution Order (Pass 2)

### Phase dependencies

- **Phase 7 Setup (T059-T061)**: No dependencies — start immediately after Pass 2 kickoff.
- **Phase 8 Foundational (T062-T064)**: Depends on Phase 7 — BLOCKS P2-SEC, P2-STB, P2-NI.
- **Phase 9 P2-SEC**: Depends on Phase 8 (needs `trust-boundaries.md`).
- **Phase 10 P2-STB**: Depends on Phase 8 (needs `crash-matrix.md`). Can run in parallel with Phase 9 (different modules and lenses, minimal file overlap).
- **Phase 11 P2-UX**: Depends on Phase 9 completion for `bot/handlers.py` (T092) to avoid merge conflicts; other UX tasks can start after Phase 8.
- **Phase 12 P2-NI**: Depends on Phase 8 (needs `string-inventory.md`). Can run in parallel with P2-SEC/STB for modules those phases don't touch.
- **Phase 13 Polish**: Depends on all preceding Pass 2 phases.

### Within-phase parallelism

- Phase 9 Group B (T070-T073) fully parallel — one adapter per developer.
- Phase 9 Group D (T074-T077) fully parallel.
- Phase 10 Group C (T084-T087) fully parallel.
- Phase 10 Group F (T088-T090) fully parallel.
- Phase 11 independent handler + AI-backend tasks (T094-T096) parallel.
- Phase 12 tone-audit tasks per module (T103-T106) fully parallel.
- Phase 13 re-evaluations (T110-T116) fully parallel.

---

## Parallel Example: Phase 9 Group B

```bash
# Three developers audit the three adapters in parallel:
Developer A → T070 telegram.py
Developer B → T071 discord.py
Developer C → T072 slack.py
Developer D → T073 base.py
```

---

## Implementation Strategy (Pass 2)

### MVP First (P2-SEC only)

1. Phase 7 Setup → baseline.
2. Phase 8 Foundational → inventories.
3. Phase 9 P2-SEC → security gaps closed. **STOP and VALIDATE**.
4. Ship as a security-hardening point release if justified.

### Incremental delivery

1. Setup + Foundational → inventories published.
2. P2-SEC → security hardened → optional point release.
3. P2-STB → crash matrix green → optional point release.
4. P2-UX + P2-NI → user-facing polish → release vX.Y+1.0 with Pass 2 summary.

### Parallel team strategy

- Phase 9 (SEC) and Phase 10 (STB) share Group A modules (handlers, scheduler, bot, ai_invoke, updater) — assign same developer to both lenses on the same module to avoid merge conflicts; parallelize across modules, not across lenses.
- Phases 11 (UX) and 12 (NI) overlap heavily on `i18n.py` and `handlers.py` — single reviewer for both on the same file.

---

## Pass 2 Notes

- `[P]` = different files, no dependencies.
- Story labels are `P2-SEC`, `P2-STB`, `P2-UX`, `P2-NI` to distinguish from Pass 1's `US1`–`US4`.
- Every fix ships with a regression test (FR-004 from spec still applies).
- Commit per module or per logical group of findings; keep commits bisectable.
- If a fix changes a user-visible string, both IT and EN `i18n` keys update in the same commit.
- Multi-platform parity (Constitution Principle I) must hold for any adapter-touching commit.
- Stop at any Pass 2 checkpoint to re-baseline and decide whether to ship.
