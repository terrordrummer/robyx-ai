# Implementation Plan: Fix Continuous Task Macro Leak

**Branch**: `004-fix-continuous-task-macro` | **Date**: 2026-04-17 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/004-fix-continuous-task-macro/spec.md`

## Summary

The `[CREATE_CONTINUOUS ...] / [CONTINUOUS_PROGRAM]...[/CONTINUOUS_PROGRAM]` macro
leaks to chat when either tag is malformed, when either tag is absent, when the
emitting agent is a workspace agent (the current handler only routes it for the
orchestrator `robyx` in the `is_robyx` branch of `_process_and_send`), when the
payload JSON is invalid, or when the LLM emits realistic variations (code
fences, curly quotes, extra whitespace). The fix consolidates macro detection,
stripping, and side-effect dispatch into a single `continuous_macro` module
invoked from every terminal response path (orchestrator, workspace agent,
collaborative group, scheduled delivery). Detection is tolerant; stripping is
unconditional (any partial match is scrubbed); side effects are gated on full
validity; the user-visible substitution is always either a short confirmation
or a short prose error. No new user-facing capability is added. Macro grammar,
state format, scheduler integration, `parent_workspace` fallback to `robyx`,
and `work_dir` confinement are preserved.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: python-telegram-bot, discord.py, slack-sdk; stdlib `re`, `json`, `pathlib`, `logging`
**Storage**: JSON state under `data/continuous/<name>/state.json` (existing; unchanged)
**Testing**: pytest (existing suite under `tests/`, 960+ tests)
**Target Platform**: Linux/macOS service process (same bot runtime on all platforms)
**Project Type**: Single-service multi-platform bot (existing layout under `bot/`, `templates/`, `tests/`)
**Performance Goals**: Macro extraction MUST run in O(n) of response length; no regressions in existing response-processing latency (<5 ms added for typical responses <16 KB).
**Constraints**: Zero leakage of macro tokens or JSON in any delivery path (chat + TTS). Must preserve all existing side effects and security invariants (`work_dir` ⊂ WORKSPACE, reserved-name rejection, `parent_workspace` → `robyx` fallback).
**Scale/Scope**: ~2 modules touched (`bot/handlers.py`, `bot/ai_invoke.py`) plus a new `bot/continuous_macro.py`; ~6 prompt templates reviewed; ~1 new test module (`tests/test_continuous_macro.py`) plus additions to `tests/test_handlers.py`, `tests/test_continuous.py`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Multi-Platform Parity | ✅ Pass | Fix lives in platform-agnostic response-processing code shared by all three adapters; no platform-specific branches added. |
| II. Chat-First Configuration | ✅ Pass | No configuration surface changes. Continuous tasks remain created from chat. |
| III. Resilience & State Persistence | ✅ Pass | Existing atomic `save_state` path reused. Dispatching the first step only after persistence. No new mutable state introduced. |
| IV. Comprehensive Testing | ✅ Pass | Every new code path gets unit + integration tests (malformed fixtures, realistic variations, per-adapter parity, scheduled-delivery leak regression). |
| V. Safe Evolution | ✅ Pass | No schema change → no migration. Behavior-compatible: every previously accepted macro still creates the task. Macro grammar additions (tolerance) are pure supersets. |

No violations. No entries in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/004-fix-continuous-task-macro/
├── plan.md               # This file
├── spec.md               # Feature specification (already authored)
├── research.md           # Phase 0 output (this run)
├── data-model.md         # Phase 1 output (this run)
├── quickstart.md         # Phase 1 output (this run)
├── contracts/            # Phase 1 output (this run)
│   ├── continuous-macro-grammar.md
│   └── extract-continuous-macros.md
├── checklists/
│   └── requirements.md   # Pre-existing
└── tasks.md              # Phase 2 output (/speckit-tasks)
```

### Source Code (repository root)

```text
bot/
├── ai_invoke.py               # MODIFY: loosen CREATE_CONTINUOUS / CONTINUOUS_PROGRAM regex (case-insensitive, curly quotes, whitespace). Re-export via continuous_macro.
├── continuous_macro.py        # NEW: single module owning detection, stripping, validation, and dispatch. Pure function `extract_continuous_macros(text) -> (stripped, [Intercepted|Rejected])` + async `apply_continuous_macros(response, ctx) -> response` that runs side effects and returns the user-visible substitution.
├── continuous.py              # UNCHANGED: state creation and lifecycle.
├── topics.py                  # UNCHANGED: `create_continuous_workspace` is the single side-effect entry point.
├── handlers.py                # MODIFY: replace the inline block at 1013–1086 with a call to `apply_continuous_macros`. Route the call from both the `is_robyx` branch AND the workspace-agent branch (currently only orchestrator is served). Ensure `_strip_executive_markers` also stripping logic is preserved for non-executive messages (unchanged behavior).
├── scheduled_delivery.py      # REVIEW + MODIFY if needed: ensure any text handed to `platform.send_message` passes through the same extraction so a late-arriving macro from a scheduled agent turn cannot leak.
├── collaborative.py           # REVIEW only: collab responses are routed through `_process_and_send`; no new hook is needed if the router already applies extraction on both executive + non-executive paths.
└── i18n.py                    # MODIFY: add localized strings for the user-visible substitutions (confirmation and the concrete error variants: bad_json, missing_field, path_denied, name_collision, permission_denied, downstream_error).

templates/
├── prompt_workspace_agent.md  # REVIEW + tighten: the grammar section MUST show ASCII straight quotes and warn the agent that typographic/curly quotes will still be accepted but ASCII is preferred. No functional change required of the agent.
├── prompt_focused_agent.md    # REVIEW: same note.
├── prompt_orchestrator.md     # REVIEW: ensure the macro is documented where the orchestrator delegates setup.
├── prompt_collaborative_agent.md  # REVIEW: non-executive collab agents MUST NOT emit the macro; tighten the prohibition.
├── CONTINUOUS_SETUP.md        # REVIEW: same ASCII-quote note.
└── CONTINUOUS_STEP.md         # UNCHANGED (step agent does not emit macros).

tests/
├── test_continuous_macro.py   # NEW: pure-function fixtures — golden, malformed (5 variants), realistic variations (code fence, curly quotes, leading/trailing prose, multiple macros, case-insensitive tags, missing closer).
├── test_handlers.py           # EXTEND: integration tests — (a) workspace-agent emission no longer leaks, (b) orchestrator emission still works, (c) collab executive branch strips, (d) malformed emission substitutes a prose error in user-visible text.
├── test_continuous.py         # EXTEND: end-to-end fixture asserting that after a valid macro the state file, branch, topic, and first scheduled step are all produced exactly once.
└── test_scheduled_delivery.py # EXTEND: regression — scheduled agent reply containing a macro is stripped before platform.send_message.
```

**Structure Decision**: Single-project Python service, existing layout under `bot/`, `templates/`, `tests/`. The new module `bot/continuous_macro.py` is a thin orchestration layer over the existing `topics.create_continuous_workspace` + `continuous.create_continuous_task` + scheduler; no new subsystem is introduced. This keeps the fix local to response processing where the leak originates (handlers.py:1013–1086).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

(No violations — section left empty.)
