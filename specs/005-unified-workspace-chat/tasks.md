---

description: "Task list for feature 005-unified-workspace-chat"
---

# Tasks: Unified Workspace Chat for Scheduled & Continuous Tasks

**Input**: Design documents from `/specs/005-unified-workspace-chat/`
**Prerequisites**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/` (3 files), `quickstart.md`

**Tests**: Test tasks are INCLUDED per Principle IV of the Robyx constitution (comprehensive testing is mandatory; every contract MUST be exercised).

**Organization**: Grouped by user story. US1 is the MVP slice. US1 and US2 are both P1 and are expected to land together before public release, but each can be implemented independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable (distinct files, no incomplete dependencies)
- **[Story]**: Owning user story (`US1`…`US5`); setup/foundational/polish phases have no story label
- All paths are repo-relative from `/Users/rpix/Workspace/products/robyx-ai/`

## Path Conventions

Single-project layout. `bot/` at repo root (NOT `src/bot/`). Tests under `tests/`. Data under `data/`. Migrations under `bot/migrations/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Scaffolding for the release-linked migration and version chain.

- [X] T001 Scaffold the migration module via `python scripts/new_migration.py 0.23.0 --from 0.22.2 --description "..."` — produces `bot/migrations/v0_23_0.py` skeleton and registers it with the chain. **Incidental**: also scaffolded the previously-missing `bot/migrations/v0_22_2.py` no-op (pre-existing gap in the chain surfaced by `test_every_release_since_0_20_12_has_a_migration_module`).
- [X] T002 Bump `VERSION` from `0.22.2` to `0.23.0` (file at repo root, single line)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared building blocks required by multiple user stories. **No user-story work begins until this phase is complete.**

- [X] T003 [P] Add icon constants and `format_delivery_message(task_type: str, task_name: str, body: str) -> str` helper in `bot/scheduled_delivery.py` per `contracts/delivery-marker.md` (icon map: 🔄/⏰/📌/🔔, 64-char name truncation with `…`, unknown-type fallback returns body unmodified and logs WARN)
- [X] T004 [P] Add `strip_control_tokens_for_user(text: str) -> str` in `bot/continuous_macro.py`, consolidating the existing `strip_continuous_macros_for_log()` logic plus `[STATUS …]` stripping; keep the old helper as a thin wrapper so the scheduled path does not regress
- [X] T005 Extend `bot/continuous.py` state read/write helpers to handle the post-0.23.0 fields (`plan_path`, `legacy_thread_id`, `migrated_v0_23_0`) with backwards-compatible defaults when reading older state files; preserve atomic write-then-rename. **Scope delivered**: added `plan_file_path()`, `write_plan_md()`, `read_plan_md()` helpers — additive only. Full schema repoint (`legacy_thread_id`, `migrated_v0_23_0`) is a US4 migration-time concern; state reads already tolerate missing fields.
- [X] T006 Create new module `bot/lifecycle_macros.py` with: macro regexes, `parse_lifecycle_macros(text) -> list[MacroInvocation]`, `handle_lifecycle_macros(invocations, ctx) -> dict[span, str]` dispatcher skeleton (handlers raise `NotImplementedError` until US2), plus `scope_to_workspace(entries, chat_id, thread_id)` helper that filters queue entries by the current workspace
- [X] T007 [P] **Relocated from `bot/ai_invoke.py` to `bot/handlers.py::_send_response`** (the actual single interactive send chokepoint; research.md R3 was imprecise about which file hosts the chokepoint). Calls `strip_control_tokens_for_user` at the top of `_send_response` so every code path that reaches the send site passes through the scrub — belt-and-suspenders on top of the existing `apply_continuous_macros` call in `_process_and_send`.
- [X] T008 [P] Wire `strip_control_tokens_for_user` into `bot/scheduled_delivery.py::_clean_result_text()` — keeps the legacy `strip_continuous_macros_for_log` call for WARN-level observability of stray-token counts on the scheduled path

**Checkpoint**: Foundation ready — user story implementation may begin.

---

## Phase 3: User Story 1 — Continuous tasks report in workspace chat (Priority: P1) 🎯 MVP

**Goal**: Creating a continuous task no longer opens a dedicated sub-topic. The first step output lands in the parent workspace chat within 2 scheduler ticks.

**Independent Test**: Per `spec.md` US1 acceptance — request a continuous task, confirm the plan, observe the workspace chat for the first step report within 2 ticks, verify no new sub-topic/thread was created on the platform.

### Tests for User Story 1

- [X] T009 [P] [US1] **Placed in `tests/test_topics.py::TestCreateContinuousWorkspaceSpec005`** (new class, 6 tests): `test_does_not_create_subtopic`, `test_state_thread_id_is_parent_thread`, `test_queue_entry_uses_parent_thread`, `test_agent_registered_with_no_thread_id`, `test_missing_parent_thread_id_returns_none`. Assert `platform.create_channel` is NEVER awaited and state `workspace_thread_id` equals the provided `parent_thread_id`.
- [X] T010 [P] [US1] `tests/test_topics.py::TestCreateContinuousWorkspaceSpec005::test_persists_plan_md` — asserts `data/continuous/<name>/plan.md` exists post-creation, content contains the rendered program (objective, criteria, constraints).
- [X] T011 [P] [US1] `tests/test_continuous_macro.py` gains a suite of `strip_control_tokens_for_user` tests (removes macro+STATUS, idempotent, handles empty/None, preserves clean text, collapses newlines). End-to-end interactive-path coverage is implicit via the existing `test_handlers.py` cases plus the `_send_response` defense-in-depth added in T007.

### Implementation for User Story 1

- [X] T012 [P] [US1] In `bot/topics.py::create_continuous_workspace()`: removed `platform.create_channel()` call; added required `parent_thread_id` parameter; removed the welcome-message send to the old sub-topic (the macro handler's `continuous_task_created` i18n line already confirms creation to the user); registered the continuous "agent" with `thread_id=None` so the parent workspace's routing is not hijacked. Kept function name for backward compatibility (rename to `create_continuous_task` is a polish-phase concern in T067).
- [X] T013 [US1] In `bot/continuous_macro.py::apply_continuous_macros()`: passes `parent_thread_id=ctx.thread_id` to `create_ws(...)`. Plan markdown rendering lives in `topics._render_plan_markdown()` (closer to the creation site) and is persisted via `continuous.write_plan_md()`; `state["plan_path"]` is populated with the relative path.
- [X] T014 [US1] Runtime callers of `create_continuous_workspace` are `continuous_macro.py` (updated in T013) and test stubs in `test_continuous_macro.py` / `test_handlers.py` — all use `**kwargs` and absorb the new parameter transparently.
- [X] T015 [US1] `bot/scheduled_delivery.py::_render_result_message()` now calls `format_delivery_message(task_type, task_name, body)` at the single delivery chokepoint — continuous-task deliveries carry `🔄 [<name>]`. Replaced the old `*<title>*\n<body>` formatting. Existing `test_scheduled_delivery.py` updated to reflect the new marker format.
- [X] T016 [US1] Full suite: **1543 passed, 1 skipped** (was 1532/1 before Increment B; +11 new tests green; no regressions).

**Checkpoint**: A new continuous task lives entirely in the parent workspace chat. No sub-topic is opened. MVP is deliverable standalone.

---

## Phase 4: User Story 2 — Primary agent manages task lifecycle (Priority: P1)

**Goal**: The primary agent lists, inspects, stops, pauses, and resumes tasks via natural language scoped to the current workspace; ambiguous commands always clarify before acting.

**Independent Test**: Per `spec.md` US2 acceptance — in a workspace with ≥2 active tasks, `lista task` returns a grouped icon summary; an ambiguous `ferma <substring>` yields a disambiguation prompt; an unambiguous `stop <name>` stops the task.

### Tests for User Story 2

- [X] T017 [P] [US2] `tests/test_lifecycle_macros.py::TestListTasks::test_empty_workspace` — asserts `"Nessun task attivo nel workspace."` for an empty scoped queue.
- [X] T018 [P] [US2] `test_grouped_summary_includes_icons_and_names` — seeds one task per type; asserts 🔄/⏰/📌/🔔 appear in order and each named task is listed.
- [X] T019 [P] [US2] `TestTaskStatus::test_single_match_returns_detailed_status` — asserts objective, status, icon, and name in the detailed render.
- [X] T020 [P] [US2] `TestStopTask::test_stop_continuous_transitions_status_to_completed` (continuous) + `test_stop_non_continuous_cancels_queue_entry` (periodic) — both mutate authoritative storage atomically.
- [X] T021 [P] [US2] `TestPauseResume::test_pause_then_resume_continuous` — roundtrip via `pause_task` / `resume_task` helpers verifies `paused` → `pending`.
- [X] T022 [P] [US2] `TestDisambiguation::test_ambiguous_substring_triggers_disambiguation_not_mutation` — two "*-report" tasks; ambiguous stop renders numbered list + "Quale intendi?" and leaves BOTH states untouched.
- [X] T023 [P] [US2] `TestDisambiguation::test_exact_match_preferred_over_substring` — exact match "foo" resolves unambiguously when "foobar" also contains "foo"; simulates the follow-up turn path.
- [X] T024 [P] [US2] `TestWorkspaceIsolation::test_other_workspace_tasks_are_invisible` — another workspace's tasks never appear in LIST or TASK_STATUS.
- [X] T025 [P] [US2] `TestTaskStatus::test_zero_match_returns_not_found` — polite "Nessun task attivo chiamato `<query>`" message.
- [X] T026 [P] [US2] `TestLogging::test_action_logged_with_resolution` — asserts one INFO log line under `robyx.lifecycle_macros` with `macro=stop_task`, task name, and outcome.
- [X] Bonus: `TestGrammar::test_parse_*` (6 tests) cover the regex grammar; `TestSubstituteMacros::*` (3 tests) cover the in-place splice used by the handlers.py wiring.

### Implementation for User Story 2

- [X] T027 [P] [US2] `_handle_list_tasks` + `render_list` — reads queue via `ctx.queue_reader` (test seam) or `scheduler.load_queue`, filters via `scope_to_workspace`, loads continuous state where applicable, groups by type in spec order with icons.
- [X] T028 [P] [US2] `_handle_task_status` + `render_status` — handles 0/1/≥2 match; detailed render for continuous tasks pulls objective / history tail / step counter from state.json.
- [X] T029 [P] [US2] `_handle_stop_task` → `_stop_task` — continuous: `continuous.complete_task` + `save_state` AND `scheduler.cancel_task_by_name`; periodic/one-shot: `cancel_task_by_name` alone. New helper `scheduler.cancel_task_by_name` added for clean queue mutation.
- [X] T030 [P] [US2] `_handle_pause_task` → `_pause_task` — continuous: `pause_task` + save; other types: friendly "non supportata" message.
- [X] T031 [P] [US2] `_handle_resume_task` → `_resume_task` — guards on status ∈ {paused, rate-limited}; uses `resume_task` helper.
- [X] T032 [P] [US2] `_handle_get_plan` → `_get_plan` — reads `data/continuous/<name>/plan.md` via `continuous.read_plan_md`; truncates to 2000 chars with "[…troncato]" tail; non-continuous types get the friendly "solo per task continuativi" message.
- [X] T033 [US2] `render_ambiguous_candidates` — shared numbered-list renderer used by stop/pause/resume/get_plan via the `_handle_mutating` higher-order wrapper.
- [X] T034 [US2] Wired into `bot/handlers.py::_process_and_send` right after `apply_continuous_macros` — lifecycle-macro handling runs on the already-stripped response; the `substitute_macros` helper performs reverse-order span splicing.
- [X] T035 [US2] `_log_action` emits INFO lines under `robyx.lifecycle_macros` with `ts=<iso>`, `workspace_thread=<thread_id>`, `macro=<kind>`, `name=<query>`, `resolved_to=<name>`, `outcome=<verb>`.
- [X] T036 [US2] Updated `templates/prompt_workspace_agent.md` (replaces the "🔄 topic" language with the new plan.md + parent-chat flow, documents the six lifecycle macros) AND `templates/prompt_orchestrator.md` (adds a matching lifecycle section). Both emphasise "never guess a name" so ambiguous queries are routed through the disambiguation prompt.

**Checkpoint**: Primary agent is the single lifecycle control point; ambiguity always resolved before mutation.

---

## Phase 5: User Story 3 — Visual marker for every scheduled delivery (Priority: P2)

**Goal**: Every scheduled delivery (continuous, periodic, one-shot, reminder) carries the type-specific icon + task name; primary conversational replies never carry a marker.

**Independent Test**: Per `spec.md` US3 acceptance — trigger one delivery per task type and verify the prefix; verify a conversational response has none.

### Tests for User Story 3

- [X] T037 [P] [US3] `tests/test_scheduled_delivery_markers.py::TestFormatDeliveryMessage::test_continuous_marker` asserts `"🔄 [daily-report] Step 3 done"`. Additional `TestRenderResultMessageMarkers::test_continuous_task_gets_rocket_icon` verifies integration through `_render_result_message`.
- [X] T038 [P] [US3] `test_periodic_marker` + `test_periodic_task_gets_alarm_icon` cover the `⏰` marker both at the helper level and at the integration level.
- [X] T039 [P] [US3] `test_oneshot_marker_all_aliases_resolve_to_same_icon` verifies `one-shot`, `oneshot`, and `one_shot` all map to `📌`; `test_oneshot_task_gets_pin_icon` covers the integration.
- [X] T040 [P] [US3] `TestReminderDispatchMarker::test_reminder_send_uses_bell_marker` drives `scheduler._dispatch_reminders` with a fake platform and asserts `kwargs["text"].startswith("🔔 [promemoria] ")`. `test_reminder_with_explicit_name_uses_that_name` asserts that when a reminder carries a `name` field the marker uses it.
- [X] T041 [P] [US3] `test_unknown_type_returns_body_unchanged_and_logs_warning` asserts the body is passed through unmodified AND a WARN log record is captured containing the unknown type string.
- [X] T042 [P] [US3] `TestConversationalRepliesUnmarked::test_strip_control_tokens_does_not_add_marker` — the interactive chokepoint (`_send_response` → `strip_control_tokens_for_user`) does NOT add any of the four icons to conversational replies (invariant verification).
- [X] T043 [P] [US3] `test_long_name_truncated_to_64_chars_with_ellipsis` — 128-char name truncated to exactly 64 chars with trailing `…`.
- [X] T044 [P] [US3] `TestSingleChokepointInvariant::test_only_two_call_sites_in_bot_package` walks `bot/**/*.py` and asserts `format_delivery_message(` appears only inside `scheduled_delivery.py` (definition + single call site in `_render_result_message`) and `scheduler.py` (single call site in `_dispatch_reminders`). Any third caller breaks the test.

### Implementation for User Story 3

- [X] T045 [US3] `bot/scheduler.py::_dispatch_reminders` now wraps `reminder["message"]` via `format_delivery_message("reminder", reminder.get("name") or "promemoria", …)` before `platform.send_message(...)`. Using the full import name (not an alias) is required for the T044 invariant check.
- [X] T046 [US3] Platform-parity is structural: the marker is applied as a plain text prefix BEFORE `platform.send_message(...)` / `platform.send_to_channel(...)`. The Platform ABC's send methods treat the text as opaque — no adapter changes required. Existing Telegram / Discord / Slack adapter tests continue to pass (verified via full suite run).
- [X] T047 [US3] `quickstart.md` §6 already specifies the real trigger path for each task type. No refinement needed based on the shipped implementation.

**Checkpoint**: All four task types deliver with correct markers across all three platforms; conversational replies are unmarked.

---

## Phase 6: User Story 4 — Migration of existing continuous tasks (Priority: P2)

**Goal**: Pre-existing continuous tasks are migrated in one idempotent pass; legacy sub-topics are closed best-effort; parent workspace chat receives exactly one transition notice per task.

**Independent Test**: Per `spec.md` US4 acceptance — with a legacy-shape `state.json` fixture, run the migration once and verify repoint + single notice + sub-topic closure; re-run and verify no additional notices and no state changes.

### Tests for User Story 4

- [ ] T048 [P] [US4] Create `tests/test_migration_v0_23_0.py::test_fresh_migration_happy_path_three_tasks` — fixture with 3 continuous tasks (2 resolvable, 1 unknown workspace); assert 2 migrated, 1 skipped, 2 transition notices via fake platform, 2 `close_channel` calls
- [ ] T049 [P] [US4] Add `test_rerun_is_noop` — rerun the same migration on already-migrated state; assert 0 new notices, 0 new `close_channel` calls, all `migrated_v0_23_0` unchanged
- [ ] T050 [P] [US4] Add `test_close_channel_failure_triggers_fallback_notice` — fake platform's `close_channel` returns `False`; assert fallback notice posted in legacy sub-topic, migration still succeeds
- [ ] T051 [P] [US4] Add `test_corrupted_state_json_is_skipped_not_fatal` — one malformed state file; assert migration logs ERROR for that task, processes remaining, returns normally
- [ ] T052 [P] [US4] Add `test_missing_workspace_skipped_with_error` — unknown `workspace_name` on one task; assert that task is skipped with ERROR log, others proceed
- [ ] T053 [P] [US4] Add `test_legacy_thread_id_equals_new_thread_id_edge` — task already on parent thread; assert no `close_channel` call, no fallback notice, transition notice still posted once, marker still stamped
- [ ] T054 [P] [US4] Add `test_offline_mode_platform_none` — `ctx.platform is None`; assert state is still repointed and marker stamped, no platform calls attempted
- [ ] T055 [P] [US4] Add `test_done_marker_file_written_after_loop` — assert `data/migrations/v0_23_0.done` exists after a completed run

### Implementation for User Story 4

- [ ] T056 [US4] Implement `bot/migrations/v0_23_0.py::upgrade(ctx)` per `contracts/migration-v0_23_0.md` algorithm — per-task loop with idempotency guard, workspace resolution, atomic state write, best-effort close, fallback notice, transition notice, summary log
- [ ] T057 [P] [US4] Add workspace-resolution helper in `bot/migrations/v0_23_0.py` (or reuse existing one from `bot/topics.py::load_workspaces()`) — returns workspace dict or None; handles both `workspace_name` and legacy `parent_workspace_name` fields
- [ ] T058 [US4] Ensure `bot/migrations/runner.py` picks up the new migration (should be automatic via module registration — verify and add an explicit import if needed)
- [ ] T059 [US4] Add `data/migrations/` directory creation to the migration routine (idempotent `Path.mkdir(parents=True, exist_ok=True)`)
- [ ] T060 [US4] Implement ISO-8601 UTC timestamp helper `_now_iso_utc()` in the migration (or import from existing utility if present)

**Checkpoint**: All pre-existing continuous tasks flow to the parent workspace chat after a single migration run; re-runs are safe.

---

## Phase 7: User Story 5 — Secondary agent knowledge parity (Priority: P3)

**Goal**: Scheduler-spawned secondary agent for continuous steps shares the primary's workspace instructions plus a task-specific plan; primary can read the plan on demand.

**Independent Test**: Per `spec.md` US5 acceptance — inspect a freshly-created continuous task's artifacts; verify secondary-agent prompt includes workspace instructions + plan.md + state.json.

### Tests for User Story 5

- [ ] T061 [P] [US5] Extend `tests/test_continuous.py` with `test_secondary_agent_prompt_includes_workspace_instructions_and_plan` — builds the prompt via the scheduler's templating code, asserts it contains the verbatim `agents/<name>.md` content AND the `plan.md` content AND the current `state.json` state summary
- [ ] T062 [P] [US5] Add `test_get_plan_macro_returns_plan_md_content` — asserts `[GET_PLAN name=...]` returns the full plan.md for a continuous task; returns a friendly message for non-continuous types

### Implementation for User Story 5

- [ ] T063 [US5] Update the secondary-agent prompt template in `bot/scheduler.py::_handle_continuous_entries()` (lines ~1031–1077 per prior explore) to load (a) `agents/<name>.md` workspace instructions, (b) `data/continuous/<name>/plan.md`, (c) current `state.json` fields (objective, history tail, status)
- [ ] T064 [US5] Ensure the load is tolerant to missing `plan.md` (older tasks pre-migration handled by T059 once migration ran; but add a one-line fallback warning + skip of that section)

**Checkpoint**: Secondary agent behavior is single-sourced with the primary; primary can answer plan questions in chat.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Release hygiene, version chain, docs, code cleanup.

- [ ] T065 Append `0.23.0` entry to `CHANGELOG.md` (summary + reference to spec 005)
- [ ] T066 [P] Create `releases/v0.23.0.md` with user-facing changes, migration behavior, rollback notes (manual-only per `contracts/migration-v0_23_0.md`)
- [ ] T067 [P] Remove `create_continuous_workspace` old name once all callers migrated (T014) — if any legacy alias remains, deprecate with a one-release grace via a warning log, or delete outright if grep shows zero external references
- [ ] T068 Run full suite: `pytest -v` from repo root; no new failures vs. `main`
- [ ] T069 Run linter: `ruff check .` (path used by project per CLAUDE.md); fix any newly-introduced issues
- [ ] T070 Update `specs/spec-status.md` with entry for 005 (status: implemented)
- [ ] T071 Smoke-test via `quickstart.md` §4–§8 on a live bot session (manual validation)
- [ ] T072 Memory hygiene — update project memory (the 5-day-old `project_continuous_tasks_design.md` memory is now superseded by this spec; leave a note or update the entry) — optional maintenance

---

## Dependencies Summary

```text
Phase 1 (Setup) ──► Phase 2 (Foundational) ──┬─► Phase 3 (US1, P1, MVP) ──┐
                                              ├─► Phase 4 (US2, P1) ──────┤
                                              ├─► Phase 5 (US3, P2) ──────┤
                                              ├─► Phase 6 (US4, P2) ──────┼─► Phase 8 (Polish)
                                              └─► Phase 7 (US5, P3) ──────┘
```

**Cross-phase coupling**:
- Phase 3 (US1) depends on T003, T005, T007, T008 (foundation).
- Phase 4 (US2) depends on T006 (lifecycle macro skeleton) and T007 (interactive chokepoint).
- Phase 5 (US3) depends on T003 (marker helper) and builds the full marker story including reminders.
- Phase 6 (US4) depends on T005 (state helpers) and requires the state schema extension from Phase 2 to be in place.
- Phase 7 (US5) depends on US1's `plan.md` persistence (T013).

## Parallel Execution Examples

### Foundation (Phase 2) — kick off in parallel
```text
T003 (marker helper)   ┐
T004 (strip helper)    ├─ no shared file ─► run in parallel
T007 (interactive wire)┘

Then sequentially:
T005 (state helpers) → T008 (scheduled wire) → T006 (lifecycle skeleton)
```

### User Story 2 tests (Phase 4) — all test tasks parallelizable
```text
T017–T026 → 10 test tasks all in tests/test_lifecycle_macros.py: run as parallel PR-ready stubs, then fill implementations
```

### User Story 3 tests (Phase 5) — run in parallel
```text
T037–T044 → 8 test tasks in tests/test_scheduled_delivery_markers.py, all independent
```

### User Story 4 tests (Phase 6) — run in parallel
```text
T048–T055 → 8 test tasks in tests/test_migration_v0_23_0.py, each with its own fixture
```

## Implementation Strategy

**MVP slice**: Phase 1 + Phase 2 + Phase 3 (US1) = **16 tasks**. This delivers the core UX fix — new continuous tasks land in the workspace chat with 🔄 markers. Shippable alone.

**Public-release slice**: add Phase 4 (US2) + Phase 5 (US3) + Phase 6 (US4) + Phase 8 (Polish). Needed so existing users do not get a mixed state, and so the primary agent becomes the usable single control point.

**Incremental rollout**:
1. Land Phase 1+2+3 behind a feature hint in logs — verify end-to-end in dev.
2. Land Phase 4 (lifecycle) to replace the sub-topic-based control.
3. Land Phase 5 (markers for non-continuous types).
4. Land Phase 6 (migration) — production-ready migration path.
5. Land Phase 7 (US5) — secondary-agent parity is a correctness guarantee, not user-visible.
6. Land Phase 8 — release tag `v0.23.0`.

## Validation

- Every task has a checkbox, ID, optional `[P]`/`[Story]` label, and an explicit file path or module reference.
- User Story 1 is independently testable per `spec.md` US1 acceptance and quickstart.md §4.
- User Story 2 independent test: `spec.md` US2 acceptance (+quickstart.md §5).
- User Story 3 independent test: `spec.md` US3 acceptance (+quickstart.md §6).
- User Story 4 independent test: `spec.md` US4 acceptance (+quickstart.md §7).
- User Story 5 independent test: `spec.md` US5 acceptance (+inspection of secondary-agent prompt per T061).
- Every contract (`delivery-marker.md`, `lifecycle-macros.md`, `migration-v0_23_0.md`) has at least one test covering it: delivery-marker → T037–T044; lifecycle-macros → T017–T026; migration → T048–T055.
- Every entity in `data-model.md` is exercised: state.json extension → T005 + T009/T010; plan.md → T010 + T013 + T061; migration markers → T055 + T049.

## Totals

- **72 tasks** total
- **Setup**: 2 · **Foundational**: 6 · **US1**: 8 · **US2**: 20 · **US3**: 11 · **US4**: 13 · **US5**: 4 · **Polish**: 8
- **MVP**: T001–T016 (16 tasks)
- **Parallel-ready**: 43 tasks carry `[P]`
