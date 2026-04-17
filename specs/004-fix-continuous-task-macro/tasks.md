---

description: "Tasks for feature 004-fix-continuous-task-macro"
---

# Tasks: Fix Continuous Task Macro Leak

**Input**: Design documents from `/specs/004-fix-continuous-task-macro/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Required by constitution Principle IV (Comprehensive Testing). Every new code path gets unit + integration coverage; every existing regression fixture that asserted raw-macro leakage is updated to assert prose substitution.

**Organization**: Tasks are grouped by user story (US1, US2, US3) so each story can be implemented and verified independently. US1 and US2 are both P1 and ship together as the MVP; US3 (P2) can follow.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Maps the task to `US1`, `US2`, `US3` from `spec.md`. Omitted for Setup, Foundational, and Polish phases.
- File paths are absolute inside the repo (`bot/...`, `tests/...`, `templates/...`, `specs/...`).

## Path Conventions

- Source: `bot/` at repo root
- Tests: `tests/` at repo root
- Fixtures: `tests/fixtures/continuous_macros/` (new)
- Prompt templates: `templates/` at repo root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Carve out the new module file and fixture directory. Zero behavior change.

- [X] T001 Create empty module file `bot/continuous_macro.py` with a module docstring and a TODO-marker block; add to `bot/__init__.py` imports only if needed (no export change required for tests to import it).
- [X] T002 [P] Create fixture directory `tests/fixtures/continuous_macros/` and stage empty placeholder files listed in `specs/004-fix-continuous-task-macro/quickstart.md` §3–§4 (`missing_program.txt`, `missing_open.txt`, `unclosed_program.txt`, `bad_json.txt`, `missing_field_objective.txt`, `path_escape.txt`, `multiple_macros_mixed.txt`, `code_fenced.txt`, `curly_quotes.txt`, `leading_prose.txt`, `mixed_case.txt`, `extra_whitespace.txt`).
- [X] T003 [P] Create empty test file `tests/test_continuous_macro.py` with `pytest` imports and a single `test_module_imports_cleanly` placeholder so the runner picks it up immediately.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Put the data-model types, the tolerant regex, and the i18n surface in place so all three user stories can build against a stable foundation. No behavior is exposed to users yet.

**⚠️ CRITICAL**: No user-story phase can land before these tasks are merged.

- [X] T004 In `bot/continuous_macro.py`, add the `RejectReason` string-enum (as a `Final` tuple or `enum.StrEnum`) and the `ContinuousMacroTokens`, `ContinuousMacroOutcome`, `ApplyContext` dataclasses exactly as specified in `specs/004-fix-continuous-task-macro/data-model.md`. Export all four names.
- [X] T005 In `bot/ai_invoke.py`, replace `CREATE_CONTINUOUS_PATTERN` and `CONTINUOUS_PROGRAM_PATTERN` with the tolerant versions per `specs/004-fix-continuous-task-macro/contracts/continuous-macro-grammar.md`: case-insensitive tag tokens (`re.IGNORECASE`), attribute-value delimiter class `["\u201C\u201D\u2018\u2019]`, `\s+` whitespace between attributes, `\s*` after the tag name. Keep the public module name unchanged (both patterns remain importable from `bot.ai_invoke`).
- [X] T006 [P] In `bot/i18n.py`, add the eight new string keys listed in `specs/004-fix-continuous-task-macro/contracts/extract-continuous-macros.md` §"i18n keys" (`continuous_task_created`, `continuous_task_error_malformed`, `continuous_task_error_bad_json`, `continuous_task_error_missing_field`, `continuous_task_error_path_denied`, `continuous_task_error_name_taken`, `continuous_task_error_permission_denied`, `continuous_task_error_downstream`). Add all localized variants (Italian, English, any other locale already present). Ensure `tests/test_i18n_parity.py` still passes.
- [X] T007 In `tests/test_continuous_macro.py`, write type-level smoke tests for the four new dataclasses/enum from T004: construction, equality, and that `ContinuousMacroOutcome.outcome` is one of `{"intercepted","rejected"}`. These MUST pass before any user-story implementation begins.

**Checkpoint**: Tolerant regex is live, types and i18n keys exist. Detection and dispatch still run through the old code path in `handlers.py:1013–1086` — nothing is wired yet.

---

## Phase 3: User Story 1 — Setup flow produces a real, running continuous task with no macro leakage (Priority: P1) 🎯 MVP (with US2)

**Goal**: When any authorized agent (orchestrator, workspace, collab-executive) emits a well-formed macro on any platform, the user sees only a short prose confirmation, and behind the scenes the topic, branch, state file, and first scheduled step are all created exactly once.

**Independent Test**: Run the interview to completion on Telegram, Discord, and Slack with both an orchestrator-emitted and a workspace-agent-emitted macro. Assert: reply contains no macro tokens or JSON; `data/continuous/<slug>/state.json` exists; git branch exists; first step is scheduled.

### Tests for User Story 1

- [X] T008 [P] [US1] In `tests/test_continuous_macro.py`, add `test_extract_golden_single_macro` — feeds a canonical opener + program block, asserts `(stripped, [tokens])` where `stripped` contains no tags and exactly one token with both spans set.
- [X] T009 [P] [US1] In `tests/test_continuous_macro.py`, add `test_apply_golden_produces_intercepted` — invokes `apply_continuous_macros` with a mock `ApplyContext` and a stub for `topics.create_continuous_workspace`, asserts one `Intercepted` outcome and that the final response contains the `continuous_task_created` i18n line (no raw tokens).
- [X] T010 [P] [US1] In `tests/test_handlers.py`, add `test_workspace_agent_macro_is_intercepted` — drives `_process_and_send` with a non-robyx agent whose `invoke_ai` stub returns a golden macro; asserts `platform.send_message` receives prose-only text and `create_continuous_workspace` was called once.
- [X] T011 [P] [US1] In `tests/test_handlers.py`, add `test_orchestrator_macro_still_works` — regression: golden macro from `robyx` still creates the task and produces the same prose confirmation (no double-interception, no duplicate creation).
- [X] T012 [P] [US1] In `tests/test_scheduled_delivery.py`, add `test_scheduled_reply_strips_macro` — a late-fired scheduled agent reply containing a golden macro reaches `platform.send_message` as prose-only text.

### Implementation for User Story 1

- [X] T013 [US1] In `bot/continuous_macro.py`, implement `extract_continuous_macros(text) -> (str, list[ContinuousMacroTokens])` — golden-path only (case-sensitive, straight quotes, no code fence). Handle multiple macros by scanning in source order. Keep detection pure (no logging, no I/O). Depends on T004+T005.
- [X] T014 [US1] In `bot/continuous_macro.py`, implement `apply_continuous_macros(response, ctx) -> (str, list[ContinuousMacroOutcome])` — golden-path only: parses JSON, validates required fields (`objective`, `success_criteria`, `first_step.description`), resolves `parent_workspace` via `ctx.manager.get_by_thread(ctx.thread_id)` with fallback to `"robyx"`, calls `topics.create_continuous_workspace`, appends one `continuous_task_created` line. Side-effect errors raise — US2 handles them. Depends on T013.
- [X] T015 [US1] In `bot/handlers.py`, replace the inline block at lines 1013–1086 inside `_handle_workspace_commands` with a single call to `bot.continuous_macro.apply_continuous_macros(response, ctx)`. Move the call OUT of `_handle_workspace_commands` and INTO `_process_and_send` so it runs **before** any other marker handler and is executed on BOTH the `is_robyx` branch AND the workspace-agent branch (the current fix for the routing gap identified in research.md R-01). Preserve `_strip_executive_markers` for non-executive participants unchanged. Depends on T014.
- [X] T016 [US1] In `bot/scheduled_delivery.py`, insert a call to `bot.continuous_macro.apply_continuous_macros` on any agent-originated text before it reaches `platform.send_message`. Construct `ApplyContext` from the scheduler's known fields (`agent`, `thread_id`, `chat_id`, `platform`, `manager`, `is_executive=True`). Log per the contract. Depends on T014.
- [X] T017 [US1] In `bot/continuous_macro.py`, add `INFO`-level logging inside `apply_continuous_macros` per `research.md` R-08: one line per outcome with `agent`, `name`, `outcome`, and (on `intercepted`) `thread_id`, `branch`. Use the existing `robyx.handlers` logger namespace (or `robyx.continuous_macro` — pick and document).

**Checkpoint**: Golden path works end-to-end on all three platforms for both orchestrator and workspace agents. Malformed inputs may still raise — that's US2.

---

## Phase 4: User Story 2 — Macro with malformed payload never leaks to the user (Priority: P1) 🎯 MVP

**Goal**: Every partial, malformed, or rejected macro produces a short prose error in the user-visible response and zero raw token or JSON leakage, regardless of which agent emitted it.

**Independent Test**: Feed each of the seven fixture files in `tests/fixtures/continuous_macros/` (see Phase 1 T002) through `apply_continuous_macros` with stubbed side effects. Assert: (a) returned response contains zero characters from the regex patterns `\[CREATE_CONTINUOUS`, `\[CONTINUOUS_PROGRAM`, `[/CONTINUOUS_PROGRAM]`, or raw JSON braces; (b) an appropriate `Rejected(reason=...)` outcome is returned; (c) the corresponding i18n key is rendered in the response.

### Tests for User Story 2

- [X] T018 [P] [US2] Populate `tests/fixtures/continuous_macros/missing_program.txt` — opener only, no program block. (fixture content, no code.)
- [X] T019 [P] [US2] Populate `tests/fixtures/continuous_macros/missing_open.txt` — program block only, no opener.
- [X] T020 [P] [US2] Populate `tests/fixtures/continuous_macros/unclosed_program.txt` — opener + `[CONTINUOUS_PROGRAM]` but no `[/CONTINUOUS_PROGRAM]`.
- [X] T021 [P] [US2] Populate `tests/fixtures/continuous_macros/bad_json.txt` — well-formed tags, program body has trailing comma.
- [X] T022 [P] [US2] Populate `tests/fixtures/continuous_macros/missing_field_objective.txt` — valid JSON but `objective` absent.
- [X] T023 [P] [US2] Populate `tests/fixtures/continuous_macros/path_escape.txt` — opener with `work_dir` pointing outside `WORKSPACE`.
- [X] T024 [P] [US2] In `tests/test_continuous_macro.py`, add `test_malformed_missing_program_strips_and_errors` — asserts `(stripped, outcomes)` where `stripped` contains none of the tag tokens and outcomes is `[Rejected(reason="malformed_missing_program")]`.
- [X] T025 [P] [US2] Add `test_malformed_missing_open_strips_and_errors` in the same file (mirrors T024 for the opposite half).
- [X] T026 [P] [US2] Add `test_unclosed_program_strips_to_end_of_response` — asserts the stripped text cuts off at the unclosed opener so JSON cannot leak.
- [X] T027 [P] [US2] Add `test_bad_json_strips_and_errors` — asserts `Rejected(reason="bad_json")` and the rendered line comes from `continuous_task_error_bad_json`.
- [X] T028 [P] [US2] Add `test_missing_field_strips_and_errors` — asserts `Rejected(reason="missing_field", detail="objective")` and the i18n format arg contains the field name.
- [X] T029 [P] [US2] Add `test_path_denied_strips_and_errors` — asserts `Rejected(reason="path_denied")`; uses monkeypatched `config.WORKSPACE`.
- [X] T030 [P] [US2] Add `test_name_taken_strips_and_errors` — stubs `topics.create_continuous_workspace` to raise `ValueError("name taken: foo")`; asserts `Rejected(reason="name_taken", name="foo")`.
- [X] T031 [P] [US2] Add `test_permission_denied_when_non_executive` — invokes `apply_continuous_macros` with `ctx.is_executive=False` on a golden macro; asserts zero side effects and a `Rejected(reason="permission_denied")` outcome; stripped text contains none of the tokens (defense-in-depth beyond `_strip_executive_markers`).
- [X] T032 [P] [US2] Add `test_downstream_error_strips_and_errors` — stubs `topics.create_continuous_workspace` to raise a generic `RuntimeError`; asserts `Rejected(reason="downstream_error")` and no exception propagates to the caller.
- [X] T033 [P] [US2] In `tests/test_handlers.py`, add `test_malformed_macro_in_handler_surfaces_prose_error` — end-to-end: `invoke_ai` stub returns a malformed macro, assert `platform.send_message` gets only the prose error string, nothing with `[CREATE_CONTINUOUS` or `{`.

### Implementation for User Story 2

- [X] T034 [US2] In `bot/continuous_macro.py`, extend `extract_continuous_macros` to record partial matches: opener-only tokens set `program_span=None`; program-only tokens set `open_span=None`; opener `[CONTINUOUS_PROGRAM]` with no closer sets `program_span.end=len(text)`. Preserve the ordering contract (by earliest-populated span start). Depends on T013.
- [X] T035 [US2] In `bot/continuous_macro.py`, extend `apply_continuous_macros` to branch on each token: emit `Rejected(malformed_missing_open | malformed_missing_program | malformed_unclosed_program)` without calling `json.loads` when either span is absent or the program is unclosed. Depends on T014+T034.
- [X] T036 [US2] In `bot/continuous_macro.py`, wrap `json.loads` in a try/except and produce `Rejected(bad_json, detail=str(e))` on failure. After parse, validate required fields (`objective` non-empty, `success_criteria` list with ≥1, `first_step.description` non-empty) and emit `Rejected(missing_field, detail=<field>)` on failure. Depends on T035.
- [X] T037 [US2] In `bot/continuous_macro.py`, resolve `work_dir` via `Path(work_dir).resolve()`, emit `Rejected(invalid_work_dir)` on `OSError/ValueError`, and `Rejected(path_denied)` if the resolved path does not start with `config.WORKSPACE.resolve()`. Depends on T036.
- [X] T038 [US2] In `bot/continuous_macro.py`, wrap the `topics.create_continuous_workspace` call in try/except: `ValueError` whose message starts with `"name taken"` → `Rejected(name_taken, name=<slug>)`; any other `ValueError` → `Rejected(downstream_error, detail=str(e))`; any other `Exception` → `Rejected(downstream_error)` with `log.error(..., exc_info=True)`. Depends on T037.
- [X] T039 [US2] In `bot/continuous_macro.py`, enforce `permission_denied` as the FIRST check inside `apply_continuous_macros`: if `ctx.is_executive is False`, every detected token becomes `Rejected(permission_denied, name=<parsed or "?">)` without any further validation or side effect. Depends on T035.
- [X] T040 [US2] In `bot/continuous_macro.py`, implement the outcome-to-line renderer: for each outcome, append exactly one line from the i18n table in `contracts/extract-continuous-macros.md`; join with `\n\n`; collapse runs of ≥3 newlines to 2. Ensure no rendered line contains `[CREATE_CONTINUOUS`, `[CONTINUOUS_PROGRAM`, `{`, `}`, or JSON payload substrings. Depends on T035–T039.

**Checkpoint**: Both P1 stories pass independently. This is the MVP. Ship here if US3 slips.

---

## Phase 5: User Story 3 — Macro embedded in varied natural-language surroundings is still intercepted (Priority: P2)

**Goal**: Realistic LLM output variations (code fences, curly quotes, leading/trailing prose, mixed case, extra whitespace, multiple macros) are intercepted with zero leakage and at least 95% creation success per SC-004.

**Independent Test**: Feed each of the five realistic-variation fixtures plus `multiple_macros_mixed.txt` through `apply_continuous_macros`. Assert the expected creation/rejection counts and zero token leakage.

### Tests for User Story 3

- [X] T041 [P] [US3] Populate `tests/fixtures/continuous_macros/code_fenced.txt` — golden macro wrapped in triple-backtick fences with nothing else inside the fence.
- [X] T042 [P] [US3] Populate `tests/fixtures/continuous_macros/curly_quotes.txt` — attribute values use `\u201C\u201D`.
- [X] T043 [P] [US3] Populate `tests/fixtures/continuous_macros/leading_prose.txt` — 100+ words of prose, macro, trailing line.
- [X] T044 [P] [US3] Populate `tests/fixtures/continuous_macros/mixed_case.txt` — tag names written `[Create_Continuous ...]` and `[continuous_program] ... [/Continuous_Program]`.
- [X] T045 [P] [US3] Populate `tests/fixtures/continuous_macros/extra_whitespace.txt` — newlines between attributes inside the opener.
- [X] T046 [P] [US3] Populate `tests/fixtures/continuous_macros/multiple_macros_mixed.txt` — one golden macro followed by one malformed macro (bad JSON).
- [X] T047 [P] [US3] In `tests/test_continuous_macro.py`, add `test_code_fenced_macro_is_intercepted_and_fences_removed` — asserts `Intercepted` outcome AND that the triple-backtick fences that wrap only the macro are also removed from `stripped`.
- [X] T048 [P] [US3] Add `test_curly_quotes_macro_is_intercepted` — asserts `Intercepted` outcome on the curly-quote fixture.
- [X] T049 [P] [US3] Add `test_leading_and_trailing_prose_preserved` — golden macro with prose on both sides; stripped text contains ONLY the prose (minus the macro) plus the appended confirmation line.
- [X] T050 [P] [US3] Add `test_mixed_case_tags_are_intercepted` — asserts case-insensitive matching on both `CREATE_CONTINUOUS` and `CONTINUOUS_PROGRAM`.
- [X] T051 [P] [US3] Add `test_extra_whitespace_between_attributes_is_tolerated` — asserts `Intercepted` with the newline-separated attribute fixture.
- [X] T052 [P] [US3] Add `test_multiple_macros_one_success_one_rejection` — asserts exactly two outcomes in source order: `Intercepted` then `Rejected(bad_json)`; response contains exactly one confirmation and one error line; no raw tokens from either macro.
- [X] T053 [P] [US3] Add `test_extract_is_idempotent` — `extract(extract(text).stripped).stripped == extract(text).stripped` for every fixture file.

### Implementation for User Story 3

- [X] T054 [US3] In `bot/continuous_macro.py`, upgrade `extract_continuous_macros` to detect a triple-backtick code fence that wraps **only** the macro (empty whitespace-plus-macro-plus-empty content) and record it in `surrounding_fence`. When stripping, remove the fence as well. Depends on T013+T034.
- [X] T055 [US3] Confirm the tolerant regex from T005 already handles mixed case and curly quotes; add a follow-up unit test ensuring the patterns match `[Create_Continuous name=\u201cfoo\u201d work_dir=\u201c/abs\u201d]` exactly once. If anything fails, tighten the regex and repeat.
- [X] T056 [US3] In `bot/continuous_macro.py`, ensure multi-macro handling preserves source order and side-effect independence: one macro's failure MUST NOT abort a later one; side effects for an earlier success MUST NOT be undone by a later failure. Covered by T052; guarantee the loop in `apply_continuous_macros` never uses `break` on error.

**Checkpoint**: All three user stories pass. Release candidate.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Prompt tightening, regression hygiene, documentation, quickstart validation. Low-risk, can land alongside or after US3.

- [X] T057 [P] In `templates/prompt_workspace_agent.md` (§ around line 178), add a one-sentence note that attribute values should use ASCII straight quotes; the bot tolerates curly quotes but ASCII is preferred. No functional change to the advertised grammar.
- [X] T058 [P] In `templates/prompt_focused_agent.md` and `templates/CONTINUOUS_SETUP.md`, make the same ASCII-quote note for consistency.
- [X] T059 [P] In `templates/prompt_collaborative_agent.md`, tighten the section that forbids non-executive agents from emitting `[CREATE_CONTINUOUS ...]` — explicitly state the macro WILL be stripped and a refusal WILL be shown, so the agent does not retry.
- [X] T060 [P] In `templates/prompt_orchestrator.md`, ensure the continuous-task delegation section still references the correct macro grammar (no change expected; this is a verification task).
- [X] T061 Delete the now-dead inline block that used to live in `bot/handlers.py:1013–1086`, as well as any orphan imports of `CREATE_CONTINUOUS_PATTERN` / `CONTINUOUS_PROGRAM_PATTERN` directly into `handlers.py` (they remain exported from `bot/ai_invoke.py` for `_EXECUTIVE_MARKERS` and `_strip_executive_markers`, which continue to use them).
- [X] T062 In `releases/` and `CHANGELOG.md`, add a patch-release entry summarizing the fix (user-facing symptom, principle guarantees, no state/schema migration).
- [X] T063 Run `python -m pytest -q` from the repo root and confirm the full suite stays green (960+ tests) plus the new `tests/test_continuous_macro.py` tests all pass.
- [X] T064 [P] Execute the manual smoke steps in `specs/004-fix-continuous-task-macro/quickstart.md` §2 (golden path) on at least one live platform adapter (Telegram). Capture the confirmation line text and log output as evidence.
- [X] T065 [P] Execute the manual smoke steps in `quickstart.md` §3 (malformed) against each fixture — confirm each produces the expected `outcome=rejected reason=...` log line and prose substitution with zero token leakage.
- [X] T066 [P] Execute the manual smoke steps in `quickstart.md` §4 (realistic variations) against each fixture — confirm each produces `outcome=intercepted` with zero leakage.
- [X] T067 Update `bot/migrations/` ONLY if any release bumps happen concurrently (this fix itself requires no migration — a no-op migration for the release version is sufficient if a bump is desired). If no version bump is taken, skip. Document the decision in the release notes from T062.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately.
- **Phase 2 (Foundational)**: Depends on Phase 1. BLOCKS all user-story phases.
- **Phase 3 (US1)**: Depends on Phase 2. Can proceed in parallel with Phase 4 IF developers are careful about `bot/continuous_macro.py` (US1 lays the golden path, US2 extends with rejection branches). Safer: serialize US1 → US2.
- **Phase 4 (US2)**: Depends on Phase 3 (it extends the functions introduced there).
- **Phase 5 (US3)**: Depends on Phase 4 (malformed + multi-macro need US2's branches; fence handling is additive).
- **Phase 6 (Polish)**: Depends on the user-story phases the particular polish task references. T063 (full suite) depends on US1 + US2 at minimum.

### Within Each User Story

- All tests inside a story are `[P]` (they live in separate test functions or fixture files) and can be written in parallel.
- Tests for US1/US2/US3 are intended to FAIL on `main` before the corresponding implementation tasks land; this is the TDD hygiene Principle IV expects.
- Implementation tasks that mutate the same file (`bot/continuous_macro.py`, `bot/handlers.py`) are NOT `[P]` and must serialize.

### Parallel Opportunities

- Phase 1: T002 and T003 are `[P]` and independent of T001.
- Phase 2: T006 is `[P]` — lives in `bot/i18n.py`, independent of `bot/continuous_macro.py` and `bot/ai_invoke.py`.
- Phase 3 tests (T008–T012): all `[P]`.
- Phase 4 fixtures (T018–T023): all `[P]` — independent files.
- Phase 4 tests (T024–T033): all `[P]`.
- Phase 5 fixtures (T041–T046): all `[P]`.
- Phase 5 tests (T047–T053): all `[P]`.
- Phase 6 template edits (T057–T060): all `[P]` — different template files.

---

## Parallel Example: User Story 1 Tests

```bash
# Launch all US1 test skeletons together (different test files / test functions):
Task: "Write test_extract_golden_single_macro in tests/test_continuous_macro.py"
Task: "Write test_apply_golden_produces_intercepted in tests/test_continuous_macro.py"
Task: "Write test_workspace_agent_macro_is_intercepted in tests/test_handlers.py"
Task: "Write test_orchestrator_macro_still_works in tests/test_handlers.py"
Task: "Write test_scheduled_reply_strips_macro in tests/test_scheduled_delivery.py"
```

---

## Implementation Strategy

### MVP (US1 + US2)

1. Complete Phase 1 (Setup).
2. Complete Phase 2 (Foundational).
3. Complete Phase 3 (US1) — golden path end-to-end.
4. Complete Phase 4 (US2) — malformed coverage.
5. Run the full test suite (Phase 6 T063).
6. Execute the quickstart smoke tests (Phase 6 T064, T065).
7. **STOP and VALIDATE**: P1 stories pass; the user-reported bug no longer reproduces.
8. Ship as a patch release.

### Incremental Delivery

- Day 1: Phase 1 + Phase 2 land together (scaffolding).
- Day 2: Phase 3 lands — golden path works for all three agent roles.
- Day 3: Phase 4 lands — malformed macros are scrubbed and substituted.
- Day 4: Phase 5 lands — realistic variations tolerated.
- Day 5: Phase 6 — polish, prompt tightening, release notes, deployment.

### Parallel Team Strategy

With two developers:

- Dev A: Phase 2 (T004–T007), Phase 3 implementation (T013–T017), Phase 4 implementation (T034–T040).
- Dev B: All fixture files (T018–T023, T041–T046), all test tasks (T008–T012, T024–T033, T047–T053), template edits (T057–T060).

This split keeps Dev B's work in test-only and fixture-only files so it never blocks Dev A on `bot/continuous_macro.py`.

---

## Notes

- Every task lists its exact file path so the work is executable without additional context.
- No task introduces a migration — the `data/continuous/<name>/state.json` schema is unchanged.
- All user-facing strings go through `bot/i18n.py` so `tests/test_i18n_parity.py` stays green.
- The tolerant regex is a superset of the current one: every response that worked before still works.
- Fixture files are plain text, no executable code — safe to commit before the implementation lands.
- Commit after each logical group (one per task is fine; one per phase is acceptable).
