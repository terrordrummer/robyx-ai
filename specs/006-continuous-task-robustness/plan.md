# Implementation Plan: Continuous-Task Observability & Lifecycle Robustness

**Branch**: `006-continuous-task-robustness` | **Date**: 2026-04-22 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/006-continuous-task-robustness/spec.md`

## Summary

Restore a clean, trustworthy user experience around continuous tasks by: (1) giving each continuous task its own dedicated messaging-platform topic with state-aware title markers and a pinned awaiting-input message; (2) replacing scheduler-driven push notifications to HQ with a pull-based append-only event journal queried via a `[GET_EVENTS]` macro consistent with the existing `[GET_PLAN]` / `[NOTIFY_HQ]` family; (3) formalising the lifecycle contract into four distinct operations (stop / resume / complete / delete with archive-on-delete); (4) eliminating stale-lock starvation with per-step heartbeat + continuous stale-lock recovery; (5) bounding workspace-close drain by a per-task configurable timeout; (6) rate-limiting orphan warnings with a 3-detection backoff that escalates once to an incident in the task's topic and journal. Telegram is the primary adapter; Discord/Slack receive graceful no-op fallbacks for topic-edit/pin/unpin until their APIs are wired.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: `python-telegram-bot` (topic edit / pin / unpin via Bot API methods `editForumTopic`, `pinChatMessage`, `unpinChatMessage`, `closeForumTopic`), `discord.py`, `slack-sdk`, stdlib `re`, `json`, `pathlib`, `logging`, `asyncio`, `dataclasses`, `fcntl`/`msvcrt` (existing atomic-write primitives)
**Storage**: JSON files under `data/` (existing pattern preserved) + new JSON-Lines event journal at `data/events.jsonl` with hourly/size-based rotation to `data/events/events-YYYYMMDD-HH.jsonl`; `data/continuous/<name>/state.json` extended with new fields (`dedicated_thread_id`, `drain_timeout_seconds`, `awaiting_pinned_msg_id`, `orphan_detect_count`, `hq_fallback_sent`, `archived_at`). No external DB. Existing SQLite `memory.db` files untouched.
**Testing**: pytest; new tests in `tests/` covering: macro parsing (`[GET_EVENTS]`), event journal append / query / rotation, platform-adapter topic-ops (Telegram + stub parity on Discord/Slack), lock heartbeat + stale detection, drain-on-close timeout honoring, orphan backoff, HQ last-resort rule, migration v0_26_0 idempotency, stop/complete/delete/resume state transitions, awaiting-input pin lifecycle, 7-day journal retention query window
**Target Platform**: macOS (launchd) + Linux (systemd) — same as existing bot deployment; no new platform target
**Project Type**: Python CLI / long-running service (single-project layout); no frontend, no new process boundary
**Performance Goals**: scheduler cadence unchanged (60 s); lock heartbeat refresh interval ≤ 60 s; awaiting-input pin visible in topic ≤ 10 s after state transition (SC-005); stale lock reclaimed ≤ 6 min after step death (SC-009); journal append latency ≤ 50 ms P95; journal query over 24 h window ≤ 500 ms P95 on typical data volumes (few thousand entries)
**Constraints**: No external database or service dependency (Principle III — Resilience); chat-first configurability of `drain_timeout_seconds` (Principle II); multi-platform parity gracefully degraded for topic primitives on Discord/Slack (Principle I — documented exception per spec Non-goals); all state changes atomic (temp-file + `os.replace`); migration v0_26_0 strictly idempotent (Principle V)
**Scale/Scope**: ≤ 20 concurrent continuous tasks realistic upper bound; journal entries ~10²–10³ per day per active task; 7-day rolling retention default (~5–30k entries steady-state); topic pool size follows continuous-task count + archived-name count, bounded by platform limits (Telegram forum topics capped per supergroup, well beyond expected scale)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### I. Multi-Platform Parity — **PASS with documented exception**

Topic primitives (editForumTopic, pinChatMessage, unpinChatMessage, closeForumTopic) exist on Telegram and are first-class. Discord supports equivalents (channel rename, pin) via discord.py. Slack lacks per-channel pinning at parity (pins are at message level and visibility is limited) and lacks dynamic channel renaming for archived-state markers.

Mitigation: platform adapter ABC extended with `edit_topic_title`, `pin_message`, `unpin_message`, `close_topic`, `archive_topic`. Telegram implements fully; Discord implements best-effort; Slack implements no-op with WARN log + documentation in release notes that the awaiting-input visibility falls back to inline `⏸ AWAITING INPUT` headers on Slack. Spec `Non-goals` explicitly calls this out; release notes MUST reiterate.

### II. Chat-First Configuration — **PASS**

All new user-facing knobs (per-task `drain_timeout_seconds`, delete vs stop vs complete operations, `[GET_EVENTS]` queries) are invocable through chat macros or existing lifecycle-macro infrastructure (`[CONTINUOUS …]`, `[STOP_TASK …]`). No new config-file requirement. Plan phase contracts define the chat surface for each operation.

### III. Resilience & State Persistence — **PASS**

- Event journal is append-only JSONL with atomic line writes (open-append, single `write()` per entry ≤ PIPE_BUF ensures atomicity on POSIX) + hourly rotation; recovery via simple tail-read.
- Heartbeat lock file uses existing atomic-rewrite primitive (`temp-file + os.replace`).
- State-file extensions are additive: missing new fields tolerated on load (defaults applied) → smooth rollback if migration reverts.
- Scheduler late-fire unchanged.
- `bot.pid` single-instance lock untouched.

### IV. Comprehensive Testing — **PASS (design-gated)**

Every new module MUST ship with tests as enumerated in Technical Context → Testing. Acceptance scenarios in spec map 1:1 to integration tests (listed in quickstart.md). Platform parity tests exercise all three adapters for each new method with the expected behaviour (full / best-effort / no-op with WARN log).

### V. Safe Evolution — **PASS**

Migration `bot/migrations/v0_26_0.py` will:
1. For each continuous task in `data/continuous/*/state.json` with `status ∈ {pending, running, awaiting-input, rate-limited}`: create a dedicated topic `[Continuous] <display_name>`, record `dedicated_thread_id` into state, update queue entry `thread_id`; re-route delivery.
2. Initialise `data/events.jsonl` (empty file; subsequent events append).
3. Seed one migration event entry per migrated task (`event_type=migration`, carrying pre-migration state snapshot for provenance).
4. Strictly idempotent: checks a `migrated_v0_26_0` timestamp field on each state; skips if present.
5. Rollback-safe: migrations that fail mid-run leave partial state recognizable by missing timestamp; re-runs complete the unfinished portion.

Release follows existing tag-based auto-update pipeline; no breaking data/ contract change (journal is additive; state extensions are additive).

**Result**: All gates pass. Proceed to Phase 0.

### Post-Design Re-Check (after Phase 1 artifacts)

Re-evaluated 2026-04-22 after writing `research.md`, `data-model.md`, `contracts/*`, `quickstart.md`.

- **I. Multi-Platform Parity** — Still PASS. `contracts/platform-topic-ops.md` specifies all five new ABC methods on all three adapters with explicit Slack UX-limitation WARN logs. Release notes obligation carried forward.
- **II. Chat-First Configuration** — Still PASS. `contracts/lifecycle-ops.md` documents both macro and slash-command surfaces for every new operation; `drain_timeout_seconds` exposed in the `[CONTINUOUS]` create macro; `[GET_EVENTS]` grammar in `contracts/events-macro.md`.
- **III. Resilience & State Persistence** — Still PASS. Journal append-only JSONL atomic at line level (research R1); heartbeat refresh via atomic temp-file+replace (contracts/lock-heartbeat.md); state extensions are additive (data-model.md §1); no external DB.
- **IV. Comprehensive Testing** — Still PASS (design-gated). 12 test modules listed in Project Structure map 1:1 to acceptance scenarios and success criteria; golden error messages in `contracts/lifecycle-ops.md` are verbatim-testable; quickstart.md §10-test-coverage-map closes the loop.
- **V. Safe Evolution** — Still PASS. `research.md` R8 and `data-model.md` §8 define `v0_26_0.py` with per-task idempotency timestamp gate, partial-failure resilience via atomic per-task state writes, and rollback tolerance (old code ignores new fields).

No new violations introduced by Phase 1 design. Ready for `/speckit.tasks`.

## Project Structure

### Documentation (this feature)

```text
specs/006-continuous-task-robustness/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature spec (from /speckit.specify, clarified)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output (entities, state transitions)
├── quickstart.md        # Phase 1 output (E2E verification walkthrough)
├── contracts/           # Phase 1 output
│   ├── events-macro.md         # [GET_EVENTS] grammar + handler contract
│   ├── event-journal.md        # JSONL schema, rotation, query contract
│   ├── platform-topic-ops.md   # Adapter ABC extensions (edit_topic_title, pin_message, etc.)
│   ├── lock-heartbeat.md       # Lock file format + heartbeat protocol
│   ├── lifecycle-ops.md        # stop / resume / complete / delete state transitions + errors
│   └── delivery-header.md      # Structured delivery message header grammar
├── checklists/
│   └── requirements.md  # Created by /speckit.specify
└── tasks.md             # Phase 2 output (/speckit.tasks command — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
bot/
├── events.py                   # NEW: event journal (append, query, rotation); task-type-agnostic schema
├── continuous.py               # EXTENDED: state-machine transitions (stop/complete/delete/resume), new state fields
├── topics.py                   # EXTENDED: dedicated-topic creation at create-time; archive_topic helper
├── scheduler.py                # EXTENDED: lock heartbeat + continuous stale-lock recovery, orphan backoff, drain-on-close (bounded), hook points for periodic/one-shot journal events
├── continuous_macro.py         # EXTENDED: clearer name_taken error message; lifecycle macro dispatch update
├── lifecycle_macros.py         # EXTENDED: stop/resume/complete/delete distinct contracts
├── ai_invoke.py                # EXTENDED: GET_EVENTS_PATTERN regex; wire into macro handler dispatch
├── handlers.py                 # EXTENDED: `_handle_get_events` handler (parallel to `_handle_notify_hq`)
├── scheduled_delivery.py       # EXTENDED: structured delivery header (step N, STATE, HH:MM, next-step preview) + awaiting-input pin flow
├── messaging/
│   ├── base.py                 # EXTENDED: ABC — edit_topic_title, pin_message, unpin_message, close_topic, archive_topic
│   ├── telegram.py             # EXTENDED: full implementation via Telegram Bot API
│   ├── discord.py              # EXTENDED: best-effort implementation (channel rename + pin)
│   └── slack.py                # EXTENDED: no-op stubs + WARN log (documented fallback)
├── migrations/
│   └── v0_26_0.py              # NEW: migrate existing continuous tasks to dedicated topics; seed events.jsonl

tests/
├── test_events_journal.py                 # NEW: append / query / rotation / retention window / task-type-agnostic schema
├── test_events_macro.py                   # NEW: [GET_EVENTS …] grammar, handler path, injection back into agent context
├── test_platform_topic_ops.py             # NEW: telegram / discord / slack parity tests for edit/pin/unpin/close/archive
├── test_lock_heartbeat.py                 # NEW: heartbeat refresh, stale detection threshold, continuous-cycle recovery
├── test_continuous_lifecycle.py           # NEW: stop/resume/complete/delete transitions + error messages (name_taken, resume_not_found)
├── test_drain_on_close.py                 # NEW: per-task drain_timeout_seconds honoured; archived-topic concurrent delete handling
├── test_orphan_backoff.py                 # NEW: 3-cycle threshold + single incident escalation + journal entry
├── test_awaiting_input_pin.py             # NEW: pin lifecycle on transition + 24h reminder + unpin on resume
├── test_delivery_header.py                # NEW: structured header grammar on every continuous delivery
├── test_hq_fallback.py                    # NEW: FR-002a — zero noise under normal conditions + exactly-one last-resort message under unreachable-topic + user-actionable
├── test_migration_v0_26_0.py              # NEW: idempotency + resumability + seeded migration events
└── test_spec_006_quickstart.py            # NEW: end-to-end quickstart scenarios

data/                            # RUNTIME (not checked in)
├── events.jsonl                 # NEW: current event journal file
├── events/                      # NEW: rotated shards
│   └── events-YYYYMMDD-HH.jsonl
└── continuous/<name>/
    └── state.json               # EXTENDED: new fields (see data-model.md)
```

**Structure Decision**: Single-project layout (existing Robyx pattern). No new top-level directories. All changes are additions to existing `bot/` modules plus one new module (`bot/events.py`) and one new migration. The test suite grows by ~12 new modules but stays in the existing `tests/` flat layout used throughout the project.

## Complexity Tracking

No Constitution Check violations to justify. Proceeding to Phase 0.
