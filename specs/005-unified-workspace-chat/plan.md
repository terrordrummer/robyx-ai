# Implementation Plan: Unified Workspace Chat for Scheduled & Continuous Tasks

**Branch**: `005-unified-workspace-chat` | **Date**: 2026-04-19 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/005-unified-workspace-chat/spec.md`

## Summary

All scheduled/continuous task output flows to the parent workspace chat with a type-specific icon marker applied in the delivery chokepoint. The dedicated continuous sub-topic (`🔄 <name>`) is eliminated at creation and retroactively migrated for existing tasks. The primary workspace agent owns lifecycle control (list/status/stop/pause/resume) scoped to its workspace, using server-processed macros to act on `queue.json` and `data/continuous/*/state.json`. Secondary (scheduled) agents keep parity with the primary by reading the same `agents/<name>.md` instructions plus a task-specific `plan.md`. A monotonic migration (`bot/migrations/v0_23_0.py`) rewrites existing continuous task state and best-effort closes legacy sub-topics via `Platform.close_channel()`.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: python-telegram-bot, discord.py, slack-sdk (via existing `bot/messaging/*` adapters); internal modules `bot/scheduler.py`, `bot/continuous.py`, `bot/continuous_macro.py`, `bot/topics.py`, `bot/scheduled_delivery.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/migrations/*`
**Storage**: JSON files under `data/` — `data/queue.json` (scheduler queue, atomic write-then-rename with `fcntl` locking), `data/continuous/<name>/state.json` (per-task state), `data/continuous/<name>/plan.md` (new, per-task plan artifact)
**Testing**: pytest (`tests/` at repo root; `pytest.ini` sets `asyncio_mode = auto`)
**Target Platform**: Linux/macOS long-running service (launchd on macOS, systemd on Linux)
**Project Type**: single-project bot (`bot/` at repo root, not `src/bot/`)
**Performance Goals**: scheduler tick every 60s (unchanged); primary agent lifecycle response within 5s p95; no increase in per-tick scheduler work vs. today
**Constraints**: atomic JSON mutations (no partial writes), multi-platform parity on Telegram/Discord/Slack, idempotent migration, no new external deps
**Scale/Scope**: per-workspace task counts in single digits typical, low double digits worst-case; existing installed base has a small number of pre-existing continuous tasks to migrate

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Applicability | Plan |
|-----------|---------------|------|
| **I. Multi-Platform Parity** | Marker prefix and sub-topic close must behave identically on Telegram/Discord/Slack. | Marker is applied once in `scheduled_delivery.py` (platform-agnostic). Sub-topic close uses existing `Platform.close_channel()` ABC (already implemented by all three adapters — verified `bot/messaging/{telegram,discord,slack}.py`). Where close is unsupported, fallback posts a final notice in the legacy sub-topic — behavior documented per adapter. |
| **II. Chat-First Configuration** | All new lifecycle operations must be reachable from chat. | list/status/stop/pause/resume exposed via natural-language intents recognized by the primary agent, translated into server-processed macros (`[LIST_TASKS]`, `[TASK_STATUS name=…]`, `[STOP_TASK name=…]`, `[PAUSE_TASK name=…]`, `[RESUME_TASK name=…]`). No file edits or dashboards required. |
| **III. Resilience & State Persistence** | Migration, thread_id repoint, marker application must survive unclean restarts. | All state mutations use existing atomic write-then-rename in `bot/continuous.py` / `bot/scheduler.py`. Migration is idempotent and runs in the v0.23.0 migration hook. Scheduler queue semantics unchanged (late-fire preserved). |
| **IV. Comprehensive Testing** | New behavior must be covered. | New tests: `tests/test_scheduled_delivery_markers.py`, `tests/test_continuous_no_subtopic.py`, `tests/test_lifecycle_macros.py`, `tests/test_migration_v0_23_0.py`, plus parity tests for each platform adapter. Existing `tests/test_continuous.py`, `tests/test_continuous_macro.py` are extended. |
| **V. Safe Evolution** | Schema change to `state.json` (thread_id repoint) and sub-topic close need a versioned migration. | One-shot `bot/migrations/v0_23_0.py`, generated via `scripts/new_migration.py`; re-runs are no-op; version bumps `VERSION` to `0.23.0` and adds `releases/v0.23.0.md` + `CHANGELOG.md` entry. |

**Gate status**: PASS. No constitutional violations. Complexity tracking section omitted.

## Project Structure

### Documentation (this feature)

```text
specs/005-unified-workspace-chat/
├── plan.md              # This file
├── spec.md              # Feature specification (already committed)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── lifecycle-macros.md       # Primary-agent macros for list/status/stop/pause/resume
│   ├── delivery-marker.md        # Marker format contract applied by delivery layer
│   └── migration-v0_23_0.md      # Migration contract & idempotency guarantees
├── checklists/
│   └── requirements.md  # Already committed
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

Repo uses a **single-project** layout with `bot/` at repository root (not `src/bot/`).

```text
bot/
├── scheduler.py                # Unified 60s loop; dispatches all task types. Touch: continuous-entry dispatch signature (no sub-topic creation), reminder delivery (add marker).
├── continuous.py               # Per-task state read/write, atomic IO. Touch: state schema repoint + add plan.md read helpers.
├── continuous_macro.py         # Macro detection & application for CREATE_CONTINUOUS / CONTINUOUS_PROGRAM. Touch: feed parent workspace thread_id into state at creation; persist plan.md at creation time.
├── topics.py                   # Workspace + continuous-workspace creation. Touch: remove sub-topic creation in create_continuous_workspace; rename to create_continuous_task (no channel).
├── scheduled_delivery.py       # Single delivery chokepoint for scheduled outputs. Touch: prepend type-specific marker (🔄/⏰/📌/🔔) + task name; strip macros on ALL paths via existing _clean_result_text helper.
├── handlers.py                 # Interactive path. Touch: final-output chokepoint for strip + reminder delivery marker for reminders path.
├── ai_invoke.py                # Final output processing for primary-agent responses. Touch: ensure strip at the single chokepoint; recognize lifecycle macros and dispatch to handler.
├── messaging/
│   ├── base.py                 # No change (ABC already has close_channel).
│   ├── telegram.py             # No change (close_forum_topic already implemented).
│   ├── discord.py              # No change (thread archive already implemented).
│   └── slack.py                # No change (conversations_archive already implemented).
└── migrations/
    └── v0_23_0.py              # New. Migrates every data/continuous/*/state.json to repoint thread_id to parent workspace thread; closes legacy sub-topic best-effort; posts one transition notice; idempotent via migration-version guard + per-task "migrated_at" stamp in state.

data/
├── queue.json                  # Unchanged schema.
└── continuous/<name>/
    ├── state.json              # Schema change: thread_id now points to parent workspace thread; add "migrated_at" (optional) field for migration idempotency.
    └── plan.md                 # NEW. Captured at creation time; readable by primary via [GET_PLAN name=…].

tests/
├── test_scheduled_delivery.py              # Existing. Extend with marker assertions.
├── test_scheduled_delivery_markers.py      # NEW. Covers marker application across all four task types.
├── test_continuous.py                      # Existing. Extend: create_continuous_task produces plan.md, no sub-topic created.
├── test_continuous_macro.py                # Existing. Extend: macro stripping covers ALL response paths.
├── test_lifecycle_macros.py                # NEW. Covers LIST_TASKS / TASK_STATUS / STOP_TASK / PAUSE_TASK / RESUME_TASK including ambiguity resolution.
├── test_migration_v0_23_0.py               # NEW. End-to-end migration + idempotency.
└── fixtures/                               # Extend with fake queue + state fixtures for lifecycle tests.

scripts/
└── new_migration.py            # Existing. Used to scaffold v0_23_0.py.

VERSION                         # Bump 0.22.2 → 0.23.0.
releases/v0.23.0.md             # NEW. Release notes.
CHANGELOG.md                    # Append 0.23.0 entry.
```

**Structure Decision**: Single-project layout with `bot/` at repo root is preserved. No new top-level packages; all changes are confined to the files listed above. The per-task `plan.md` artifact lives alongside the existing `state.json` under `data/continuous/<name>/` to keep per-task state self-contained.

## Complexity Tracking

*No constitutional violations to justify — section intentionally empty.*
