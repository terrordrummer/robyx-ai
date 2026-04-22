---

description: "Task list for spec 006 — Continuous-Task Observability & Lifecycle Robustness"
---

# Tasks: Continuous-Task Observability & Lifecycle Robustness

**Input**: Design documents from `/specs/006-continuous-task-robustness/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (events-macro, event-journal, platform-topic-ops, lock-heartbeat, lifecycle-ops, delivery-header), quickstart.md

**Tests**: Tests are REQUIRED for this feature. Constitution Principle IV mandates coverage of every public contract (platform adapters, scheduler paths, migrations, handlers) with golden-path + at least one error case. Each story ships its own tests; integration tests live in final polish phase.

**Organization**: Tasks are grouped by user story (US1–US4 per spec.md) to enable independent implementation and testing. Story priorities: US1=P1, US2=P1, US3=P2, US4=P2. MVP = US1 + US2 (the two P1 stories).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

## Path Conventions

- Single project: `bot/` (source), `tests/` (tests) at repository root — matches Robyx layout
- Runtime data: `data/` (gitignored)
- Migration: `bot/migrations/v0_26_0.py`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Wiring and config scaffolding prior to any feature code.

- [ ] T001 Add environment-variable config knobs with defaults in `bot/config.py`: `LOCK_HEARTBEAT_INTERVAL_SECONDS=30`, `LOCK_STALE_THRESHOLD_SECONDS=300`, `ORPHAN_INCIDENT_THRESHOLD=3`, `EVENT_RETENTION_DAYS=7`, `EVENT_MAX_HOT_BYTES=10485760`, `AWAITING_REMINDER_SECONDS=86400`, `DRAIN_TIMEOUT_DEFAULT_SECONDS=3600`, `TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS=300`
- [ ] T002 [P] Create skeleton for migration `bot/migrations/v0_26_0.py` with `describe()` / `apply(data_dir)` / `is_applied(data_dir)` entrypoints matching existing migration pattern (reference `bot/migrations/v0_23_0.py`); body is a TODO stub — populated progressively by US2/US3/US4 tasks
- [ ] T003 [P] Ensure `data/events/` directory creation on bot startup in `bot/bot.py` (idempotent `mkdir(parents=True, exist_ok=True)`)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Infrastructure required by ALL user stories. No story can begin until this phase is complete.

**⚠️ CRITICAL**: Story work starts ONLY after Phase 2 checkpoint.

### Event journal (used by US1 reads, US2/US3/US4 writes)

- [ ] T004 Create `bot/events.py` with `append()`, `query()`, `rotate_if_needed()`, `prune_retention()` per `contracts/event-journal.md`; JSONL format at `data/events.jsonl`; process-wide threading lock; atomic line-level append
- [ ] T005 [P] Create `tests/test_events_journal.py` covering: append single + concurrent entries, query by time window, query by task filter, query by type filter, `limit` clamping, rotation on hour change, rotation on 10 MB size, retention prune after 7 days, corrupted-line tolerance, task-type-agnostic schema (entries with each of `continuous|periodic|one-shot|reminder` round-trip intact)

### State schema extensions + state-machine

- [ ] T006 Extend `bot/continuous.py` `ContinuousTask` state schema per `data-model.md` §1: add fields `dedicated_thread_id`, `drain_timeout_seconds`, `awaiting_since_ts`, `awaiting_pinned_msg_id`, `awaiting_reminder_sent_ts`, `orphan_detect_count`, `orphan_last_detected_ts`, `hq_fallback_sent`, `topic_unreachable_since_ts`, `archived_at`, `migrated_v0_26_0`; tolerant loader defaults missing fields; legacy hyphen-form status (`awaiting-input`, `rate-limited`) accepted on read and rewritten to underscore form on next save
- [ ] T007 Create `bot/continuous_state_machine.py` with `ContinuousStatus` enum (`created`, `running`, `awaiting_input`, `rate_limited`, `stopped`, `completed`, `error`, `deleted`), `TRANSITIONS` table, `validate_transition(current, target)` function, and `normalize_legacy_status(str) -> str` helper. Contract: `contracts/lifecycle-ops.md` state diagram
- [ ] T008 [P] Create `tests/test_continuous_state_machine.py`: valid transitions, invalid transitions (raises `InvalidTransition`), legacy-to-underscore normalisation for `awaiting-input` and `rate-limited`, idempotent terminal states

### Platform ABC extensions + adapter implementations

- [ ] T009 Extend `bot/messaging/base.py` `Platform` ABC with `edit_topic_title`, `pin_message`, `unpin_message`, `close_topic`, `archive_topic` abstract methods per `contracts/platform-topic-ops.md`; add `TopicUnreachable` exception class with `channel_id` and `reason` attributes
- [ ] T010 [P] Implement the 5 new methods in `bot/messaging/telegram.py` via Bot API calls (`editForumTopic`, `pinChatMessage`, `unpinChatMessage`/`unpinAllForumTopicMessages`, `closeForumTopic`); map `TOPIC_ID_INVALID` / `TOPIC_CLOSED` to `TopicUnreachable`; include 3-attempt exponential backoff (0.5s, 1s, 2s) on transient errors
- [ ] T011 [P] Implement the 5 new methods in `bot/messaging/discord.py` via discord.py (`channel.edit`, `message.pin/unpin`, `thread.edit(archived=True, locked=True)`); map `discord.NotFound` to `TopicUnreachable`
- [ ] T012 [P] Implement the 5 new methods in `bot/messaging/slack.py` via slack-sdk (`conversations.rename`, `pins.add/remove`, `conversations.archive`); log one WARN per method per session documenting Slack UX caveats (workspace-wide pins, permanent archive)
- [ ] T013 [P] Create `tests/test_platform_topic_ops.py` parity suite: for each of 5 methods × 3 adapters, assert happy-path, `TopicUnreachable` mapping, transient-error retry, Slack first-call WARN-log emission

### Lock heartbeat primitives

- [ ] T014 Extend `bot/scheduler.py`: add `_write_heartbeat(lock_path, pid)` helper (atomic temp-file + os.replace with two-line `pid\niso_ts`); add `LockStatus` enum (`ALIVE`, `STALE_DEAD_PID`, `STALE_ZOMBIE`, `MISSING`); rewrite `check_lock()` to return `LockStatus` per `contracts/lock-heartbeat.md` (pid-only legacy locks remain ALIVE while pid lives; two-line locks stale if heartbeat > `LOCK_STALE_THRESHOLD_SECONDS`)
- [ ] T015 [P] Create `tests/test_lock_heartbeat.py`: heartbeat refresh writes both lines atomically, scheduler detects stale-dead-pid, scheduler detects stale-zombie, legacy pid-only lock treated as alive, MISSING status for absent lock, all-null safety on malformed lock content

### Delivery header (single chokepoint; used by every delivery)

- [ ] T016 Extend `bot/scheduled_delivery.py` `_render_result_message`: prepend structured header per `contracts/delivery-header.md` (`icon [name] · Step N/M? · state_emoji state_label · HH:MM`) plus optional `→ Next:` preview line; state_label derived from task.status; defensive strip of any agent-embedded header matching the regex before prepending
- [ ] T017 [P] Create `tests/test_delivery_header.py`: 100 consecutive deliveries regex-match `DELIVERY_HEADER_RE`, exhaustive state-to-emoji mapping, rate-limited `HH:MM` formatting, next-step 80-char truncation with ellipsis, agent-embedded-header stripping (defensive path)

**Checkpoint**: Event journal, state machine, platform adapters, lock heartbeat, delivery header are all wired and tested. User story work can now begin in parallel.

---

## Phase 3: User Story 1 — Silent Scheduler + Pull-Based Event Journal (Priority: P1) 🎯 MVP

**Goal**: Zero scheduler-driven push notifications in HQ. HQ orchestrator can query the event journal on demand via `[GET_EVENTS]` and render an accurate summary.

**Independent Test**: Start 2 concurrent continuous tasks for 120 min; verify HQ receives zero automatic messages from scheduler activity (dispatches, lock recoveries, orphan detections, state transitions). Query `[GET_EVENTS since="2h"]` and receive a chronologically ordered summary covering every journaled event in the window.

### Tests for User Story 1

- [ ] T018 [P] [US1] Create `tests/test_events_macro.py` covering `[GET_EVENTS]` grammar: valid attr combinations parse, `since` accepts both durations (`30m`, `2h`, `1d`, `3600s`) and ISO-8601, `limit` clamped to [1, 1000], malformed attrs produce structured error-injection back to agent context (not to user chat), handler strips the macro token from outward response, handler logs INFO on invocation
- [ ] T019 [P] [US1] Create `tests/test_hq_silence.py`: simulate scheduler cycles with dispatches, completions, orphan recoveries, state transitions; assert `platform.send_message` to HQ (chat_id + `thread_id=1`/control_room) is called 0 times
- [ ] T020 [P] [US1] Create `tests/test_hq_fallback.py` covering FR-002a last-resort surface: (a) normal conditions → zero HQ messages; (b) unreachable topic + user-actionable event (awaiting_input | error | task_death) → exactly one HQ message per episode (deduplicated via `hq_fallback_sent` flag); (c) unreachable topic + routine event → still zero HQ messages

### Implementation for User Story 1

- [ ] T021 [US1] Add `GET_EVENTS_PATTERN` regex to `bot/ai_invoke.py` alongside existing `NOTIFY_HQ_PATTERN`; register in any macro-dispatch list the module exposes
- [ ] T022 [US1] Implement `_handle_get_events` in `bot/handlers.py` (parallel to `_handle_notify_hq`): strip all macro tokens from outgoing response; parse attrs via existing `_COLLAB_ATTR_PATTERN`; call `bot.events.query(...)`; serialise result into markdown table; inject as system-role context message for same turn; error-inject on malformed attrs with codes `INVALID_DURATION`, `UNKNOWN_TASK`, `INVALID_LIMIT`, `WINDOW_TOO_LARGE`
- [ ] T023 [US1] Wire `_handle_get_events` into the handler dispatch chain in `bot/handlers.py` (register in the patterns tuple near `("NOTIFY_HQ", NOTIFY_HQ_PATTERN)`)
- [ ] T024 [US1] Emit `dispatched` journal event from `bot/scheduler.py` at each `_handle_continuous_entries` dispatch site (replace current bot.log-only `append_log("%s -- DISPATCHED -- ...")` with both existing log AND `events.append(task_name, "continuous", "dispatched", "ok", {"pid": N, "step": N, "model": "..."})`)
- [ ] T025 [US1] Emit `step_complete`, `state_transition`, `orphan_detected`, `orphan_recovery`, `rate_limited`, `rate_limit_recovered`, `lock_recovered` journal events from `bot/scheduler.py` at their respective code paths; consolidated helper `_journal_scheduler_event(task, event_type, outcome, payload=None)` to avoid duplication
- [ ] T026 [US1] Remove any direct HQ-push side effect from scheduler code paths: audit `bot/scheduler.py` for `platform.send_message(chat_id=HQ, ...)` or `send_to_channel(control_room_id, ...)` calls triggered by dispatch/completion/state-transition events; convert every such call into a journal append (unless it is an FR-002a last-resort path — implemented in T031)
- [ ] T027 [US1] Add scheduler rotation hook: call `bot.events.rotate_if_needed()` once per scheduler cycle; call `bot.events.prune_retention()` once per day (tracked by last-run timestamp in `data/state.json` or equivalent)
- [ ] T028 [US1] Extend `bot/bot.py` startup to register a no-op handler for any pre-existing `[GET_EVENTS]` in persisted state (forward-compat safety) and ensure `data/events.jsonl` is created empty if missing

### FR-002a last-resort HQ surface

- [ ] T029 [US1] Add `_attempt_topic_recovery(task, platform)` helper in `bot/scheduler.py`: on `TopicUnreachable` exception during delivery, first try `platform.create_channel("[Continuous] <display_name>")` with up to 3 attempts in `TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS`; on success → update `dedicated_thread_id`, clear `topic_unreachable_since_ts`, journal `lock_recovered` / `topic_recreated` event; on failure → set `topic_unreachable_since_ts` if not set
- [ ] T030 [US1] Add `_should_fallback_to_hq(task, pending_event_type)` helper in `bot/scheduler.py`: returns True iff (i) `topic_unreachable_since_ts` is set AND (ii) `pending_event_type in {awaiting_input, error, task_death}` AND (iii) `hq_fallback_sent` is False
- [ ] T031 [US1] Implement FR-002a last-resort HQ post in `bot/scheduler.py`: when `_should_fallback_to_hq` returns True, call `platform.send_message(chat_id=CHAT_ID, thread_id=platform.control_room_id, text=<structured fallback message>)`; set `hq_fallback_sent=True`; emit `hq_fallback_sent` journal event; no other paths may post to HQ automatically

**Checkpoint**: US1 is fully functional and testable. Running `[GET_EVENTS since="2h"]` from HQ returns journaled events; scheduler activity does not surface in HQ. MVP increment ready for validation.

---

## Phase 4: User Story 2 — Dedicated State-Aware Topic per Continuous Task (Priority: P1)

**Goal**: Every continuous task has its own Telegram topic with a live state marker in the title, a pinned structured awaiting-input message, and structured headers on every delivery.

**Independent Test**: Create a continuous task; verify dedicated topic appears with `[Continuous] <name> · ▶` title; run a step that ends awaiting-input; verify pinned message in the topic, title becomes `· ⏸`, parent workspace topic received zero messages; reply to resume, verify unpin + title reverts to `· ▶`; after 24h of continued silence exactly one reminder appears.

### Tests for User Story 2

- [ ] T032 [P] [US2] Create `tests/test_dedicated_topic_creation.py`: `create_continuous_workspace` now creates a new topic `[Continuous] <display_name>`, stores `dedicated_thread_id` in state, queue entry `thread_id` points to the new topic (not parent); parent workspace thread receives zero continuous-task messages in a simulated 2-hour workload
- [ ] T033 [P] [US2] Create `tests/test_topic_state_marker.py`: topic title suffix updates on every state transition (running→⏸/⏳/⏹/✅/❌ per `data-model.md` §4 mapping); transitions batched within a scheduler cycle produce one final title update per cycle (no intermediate flicker)
- [ ] T034 [P] [US2] Create `tests/test_awaiting_input_pin.py`: on transition to awaiting_input a pinned message appears within 10s carrying the question verbatim + instruction; state carries `awaiting_pinned_msg_id`; resume unpins and clears the field; after 24h (use short-circuited `AWAITING_REMINDER_SECONDS=1` in fixture) exactly one reminder posts referencing the original question; second awaiting-input episode after resume starts fresh reminder cycle

### Implementation for User Story 2

- [ ] T035 [US2] Modify `bot/topics.py` `create_continuous_workspace` to create a dedicated topic via `platform.create_channel("[Continuous] <display_name>")`, store the returned id as `dedicated_thread_id`, update queue entry `thread_id` to the new id (not `parent_thread_id`); retain `parent_thread_id` on state for reference / cleanup; keep `description="[Continuous] <display_name>"` on agent registration
- [ ] T036 [US2] Modify `bot/scheduled_delivery.py` `deliver_task_output` to route all continuous-task deliveries to `task.dedicated_thread_id` (fallback to queue entry `thread_id` only if `dedicated_thread_id` is None, preserving behaviour for non-migrated tasks on first boot)
- [ ] T037 [US2] Add `_update_topic_state_marker(task, platform)` helper in `bot/continuous.py` invoked on every state transition: computes suffix from `ContinuousStatus` per `data-model.md` §4 mapping; calls `platform.edit_topic_title(dedicated_thread_id, f"[Continuous] {display_name}{suffix}")`; catches `TopicUnreachable` → triggers recovery path from T029
- [ ] T038 [US2] Add `_pin_awaiting_message(task, platform, message_id)` and `_unpin_awaiting_message(task, platform)` helpers in `bot/continuous.py`; called by `scheduled_delivery` on transition to/from `awaiting_input`; store `awaiting_pinned_msg_id` atomically
- [ ] T039 [US2] Set `awaiting_since_ts = now_utc()` on every transition to `awaiting_input`; clear it + `awaiting_pinned_msg_id` + `awaiting_reminder_sent_ts` on resume (in the state-machine transition handler)
- [ ] T040 [US2] Add 24h awaiting-input reminder loop in `bot/scheduler.py`: per cycle, for each task with `status=awaiting_input` AND `awaiting_reminder_sent_ts is None` AND `now - awaiting_since_ts >= AWAITING_REMINDER_SECONDS`: post one reminder message `⏸ Still awaiting your reply: <question>` to `dedicated_thread_id`; set `awaiting_reminder_sent_ts = now_utc()`; journal `awaiting_reminder_sent` event (new event_type to register in contracts/event-journal.md taxonomy if not already present — add it to `bot/events.py` `_KNOWN_EVENT_TYPES` allowlist)
- [ ] T041 [US2] Extend migration `bot/migrations/v0_26_0.py` to create dedicated topics for all existing `data/continuous/*/state.json` whose `dedicated_thread_id is None` AND `status != deleted`: per task, resolve parent thread_id, call `create_channel("[Continuous] <display_name>")`, update state (`dedicated_thread_id`, `migrated_v0_26_0=now_utc()`), update queue entry `thread_id`, seed one `migration` event with payload `{"old_thread_id": <parent>, "new_thread_id": <dedicated>}`, set title with current state marker, if task is awaiting_input retroactively post + pin its question
- [ ] T042 [P] [US2] Create `tests/test_migration_v0_26_0.py`: apply on synthetic set of 3 existing tasks (1 running, 1 awaiting_input, 1 stopped), verify each gets dedicated topic + migration event + correct title suffix; re-apply → no-op (idempotency gate); simulate crash mid-migration (first task done, second interrupted) then re-run → completes second + third only

**Checkpoint**: US2 is fully functional. Continuous deliveries land exclusively in dedicated topics with structured headers and live state markers. Awaiting-input is visually unmistakable. Existing tasks are migrated.

---

## Phase 5: User Story 3 — Unambiguous Lifecycle: stop / resume / complete / delete (Priority: P2)

**Goal**: Four distinct lifecycle operations with clear user-visible semantics, golden error messages on misuse, and archive-on-delete that preserves topic history while freeing the name.

**Independent Test**: Create `lifecycle-X`; run `[STOP_TASK]` → task remains resumable, topic preserved with `· ⏹`; `[RESUME_TASK]` → back to running; `[STOP_TASK]` then `[CONTINUOUS name="lifecycle-X" ...]` → error message matches golden `name_taken` (identifies state, tells user to delete or rename); `[DELETE_TASK]` → topic renamed `[Archived] lifecycle-X` and closed, name freed; fresh `[CONTINUOUS name="lifecycle-X" ...]` succeeds with new topic; `[RESUME_TASK name="lifecycle-X-old-deleted"]` → golden `resume not_found` message.

### Tests for User Story 3

- [ ] T043 [P] [US3] Create `tests/test_continuous_lifecycle.py` covering all transitions from `contracts/lifecycle-ops.md` state diagram (stop, resume, complete, delete) with pre/post-condition assertions: stop preserves state+history+topic, resume clears awaiting, complete terminal+no-dispatch, delete archives+frees-name
- [ ] T044 [P] [US3] Create `tests/test_lifecycle_error_messages.py` asserting golden error messages verbatim: `name_taken` on create with reserved name (covers all 4 non-deleted states: stopped, completed, running, awaiting_input), `resume not_found` on missing task, `invalid_state` on resume of a running task, `terminal_state` on stop/complete of a completed/deleted task

### Implementation for User Story 3

- [ ] T045 [US3] Rewrite `bot/lifecycle_macros.py` `_stop_task` per `contracts/lifecycle-ops.md`: `status→stopped`, gracefully SIGTERM running subprocess with `drain_timeout_seconds` budget, unpin awaiting message if present, update topic title to `· ⏹`, append `stopped` event; idempotent second stop returns `noop` journal event + user-visible "already stopped"
- [ ] T046 [US3] Rewrite `bot/lifecycle_macros.py` `_resume_task` per contract: only valid from `stopped` or `awaiting_input`; clears awaiting_* fields, unpins, title→`· ▶`, next scheduler cycle dispatches; on missing task emit golden `resume not_found` message including recreate and `[GET_EVENTS]` history pointer
- [ ] T047 [US3] Add `_complete_task` in `bot/lifecycle_macros.py`: `status→completed`, terminate subprocess gracefully, title→`· ✅`, post final completion delivery to dedicated topic, append `completed` event; idempotent; reject from already-terminal states
- [ ] T048 [US3] Add `_delete_task` in `bot/lifecycle_macros.py` per contract: (1) terminate subprocess bounded by `drain_timeout_seconds`; (2) post final `· ✅ completed` delivery with "task deleted, topic archiving" body; (3) `platform.archive_topic(dedicated_thread_id, display_name)`; (4) remove `agents/<name>.md`; (5) cancel queue entries; (6) `status=deleted`, `archived_at=now`; (7) `manager.remove_agent(name)`; (8) append `deleted` + `archived` events; name immediately free
- [ ] T049 [US3] Update `bot/continuous_macro.py` `name_taken` rejection path to produce the golden error message including the current state of the reserving task and the concrete next step: `[DELETE_TASK name="X"]` to free the name, or choose a different name
- [ ] T050 [US3] Add `/stop`, `/resume`, `/complete`, `/delete` slash-command handlers in `bot/handlers.py` (reuse `_handle_stop_task` etc. — macros and slash share the same implementation); register in the slash-command dispatcher
- [ ] T051 [P] [US3] Create `tests/test_lifecycle_slash_commands.py`: each of `/stop`, `/resume`, `/complete`, `/delete` produces identical state transitions as the equivalent macro; invoked from a workspace topic, confirmation message appears in that topic

**Checkpoint**: US3 fully functional. Lifecycle is unambiguous. User can reuse a name after `/delete` without any manual cleanup.

---

## Phase 6: User Story 4 — Robust Execution (heartbeat, drain, orphan backoff) (Priority: P2)

**Goal**: Tasks recover automatically from subprocess death within minutes (no bot restart). Workspace close never loses in-flight output. Repeated crashes escalate to a single incident message rather than log spam.

**Independent Test**: (a) SIGKILL a running step → scheduler reclaims lock + dispatches a fresh step within 6 minutes. (b) Close parent workspace mid-step → step drains within `drain_timeout_seconds` and delivers with `⚠ workspace closed` header. (c) Reproducible-crash step → after 3 consecutive orphan detections one incident message appears in task topic + `orphan_incident` journal entry + task transitions to `error`; no HQ push; no further warning spam.

### Tests for User Story 4

- [ ] T052 [P] [US4] Create `tests/test_lock_heartbeat_recovery.py`: start step subprocess with heartbeat loop; kill -9; scheduler on next cycle detects `STALE_ZOMBIE` or `STALE_DEAD_PID`, cleans lock, dispatches fresh step; verify total recovery within 6 min with `LOCK_HEARTBEAT_INTERVAL_SECONDS=1` + `LOCK_STALE_THRESHOLD_SECONDS=3` in fixture
- [ ] T053 [P] [US4] Create `tests/test_drain_on_close.py`: close parent workspace during a running step; step allowed to finish within `drain_timeout_seconds` and delivers with `⚠ workspace closed` marker to dedicated topic; separately — if task is concurrently deleted mid-drain → output recorded to journal as full-body event + reference message posted to archived topic (before archival completes) OR journal-only (after archival); drain timeout exceeded → subprocess terminated, `drain_timeout` journal event, user-notified in dedicated topic
- [ ] T054 [P] [US4] Create `tests/test_orphan_backoff.py`: reproducible-crash step crashes 3 cycles in a row → on cycle 3 exactly one incident message in dedicated topic (body matches expected format from `contracts/delivery-header.md`), one `orphan_incident` journal entry with full `OrphanIncidentPayload` (last_exit_code, last_output_tail, lock_last_heartbeat_ts, detected_cycles=3, dedicated_thread_id), task transitions to `error`, no subsequent warnings logged; resumable via `[RESUME_TASK]` which resets `orphan_detect_count=0`

### Implementation for User Story 4

- [ ] T055 [US4] Implement subprocess-side heartbeat loop in the continuous-step worker entry point (locate in `bot/ai_invoke.py` spawn site or create `bot/continuous_worker.py` if not present): daemon thread calls `_write_heartbeat(lock_path, pid)` every `LOCK_HEARTBEAT_INTERVAL_SECONDS`; stops on normal exit or SIGTERM
- [ ] T056 [US4] Modify `bot/scheduler.py` `_handle_continuous_entries` to call extended `check_lock()` (returns `LockStatus`): on `STALE_DEAD_PID` → delete lock + journal `lock_recovered` (outcome=`stale_dead_pid`); on `STALE_ZOMBIE` → SIGTERM + 5s grace + SIGKILL + delete lock + journal `lock_recovered` (outcome=`stale_zombie`); on `ALIVE` → skip; on `MISSING` → treat as orphan check path
- [ ] T057 [US4] Orphan backoff logic in `bot/scheduler.py`: on detecting `status==running` + `LockStatus.MISSING`, increment `orphan_detect_count` and set `orphan_last_detected_ts`; if `orphan_detect_count < ORPHAN_INCIDENT_THRESHOLD` (3) → journal `orphan_detected` + mark step failed silently; if `>= 3` AND consecutive (each within 2 × scheduler cycle) → trigger incident (next task); if `now - orphan_last_detected_ts > 2*cycle` → reset counter to 1
- [ ] T058 [US4] Orphan incident escalation in `bot/scheduler.py`: read last 500 bytes of `data/<name>/output.log`; journal `orphan_incident` with full `OrphanIncidentPayload`; transition `status→error`; post single structured incident message to dedicated topic via `scheduled_delivery` (use delivery-header state `❌ error`); after incident, no further orphan warnings for this task until reset via resume or recreation
- [ ] T059 [US4] Orphan recovery path: on any successful `step_start` after an orphan detection, reset `orphan_detect_count=0` and journal `orphan_recovery`
- [ ] T060 [US4] Drain-on-close in `bot/scheduler.py` `cancel_tasks_for_agent_file`: add `drain=True` parameter; when draining a continuous task with a running step, wait up to `task.drain_timeout_seconds` for subprocess; on normal exit deliver output with `⚠ workspace closed` header via `scheduled_delivery`; on timeout → SIGTERM + 5s + SIGKILL + journal `drain_timeout` + post user-notification with `⏱ drain timeout` header
- [ ] T061 [US4] Drain-during-delete handling: if task is concurrently `deleted` mid-drain (archive in progress), write final step output as full-body `step_complete` journal event (payload carries truncation up to 8 KB) + post single reference message `drain output recorded in journal — see archived topic or [GET_EVENTS]` as last line to dedicated topic before archival completes; if archival has already completed, journal-only delivery and skip the reference message
- [ ] T062 [US4] Expose `drain_timeout_seconds` via `[CONTINUOUS name="X" drain_timeout="<duration>"]` macro attr in `bot/continuous_macro.py`: parse duration string (`1h`, `3600s`), validate range [60, 7200], store in state at create time; default `DRAIN_TIMEOUT_DEFAULT_SECONDS` if absent

**Checkpoint**: US4 fully functional. Autonomous tasks survive subprocess crashes and workspace closures without manual intervention. Orphan spam eliminated.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, migration validation, end-to-end integration, and release preparation.

- [ ] T063 [P] Update `CHANGELOG.md` with v0.26.0 entry summarising spec 006 (dedicated topics, event journal, lifecycle contract, heartbeat + drain + backoff); reference spec dir
- [ ] T064 [P] Create `releases/v0.26.0.md` release notes including: migration v0_26_0 behaviour, known Slack UX caveats (workspace-wide pins, permanent archive), breaking-change summary (none, all extensions are additive), operator guide for the new env vars
- [ ] T065 [P] Update `AGENTS.md` and `ORCHESTRATOR.md` with documentation of the new macros (`[GET_EVENTS]`, `[STOP_TASK]`/`[RESUME_TASK]`/`[COMPLETE_TASK]`/`[DELETE_TASK]`, `[CONTINUOUS drain_timeout="..."]`) and the new lifecycle contract; include example prompts for orchestrator usage
- [ ] T066 [P] Update `templates/CONTINUOUS_SETUP.md` (agent instructions for continuous tasks) so step agents know they can emit structured awaiting-input questions and the delivery layer will pin+mark them
- [ ] T067 Create `tests/test_spec_006_quickstart.py` end-to-end harness that scripts the quickstart.md §1–§9 scenarios programmatically (§10 is a manual 7-day live run and is documented but not automated)
- [ ] T068 Run `bot/migrations/v0_26_0.py apply` on a fresh data dir and on an established data dir with pre-existing continuous tasks; verify idempotency + correctness against `data-model.md` §8
- [ ] T069 Performance sanity: run `tests/test_events_journal_perf.py` (add if not present — append latency P95 ≤ 50 ms, query over 24h window P95 ≤ 500 ms on 10 000 entries, rotation time ≤ 100 ms)
- [ ] T070 [P] Grep audit: `grep -rn "append_log.*DISPATCHED\|append_log.*ERROR\|platform.send_message" bot/scheduler.py` — every match is either gated by FR-002a or has a matching journal emission
- [ ] T071 [P] Cross-adapter parity audit: run `tests/test_platform_topic_ops.py` against all three adapters; confirm Slack WARN-log emissions are accurate and only emitted once per method per session
- [ ] T072 Bump `VERSION` to `0.26.0` and tag; per Robyx release flow (auto-update pipeline, pre-update snapshot, smoke test, atomic rollback); verify migration `v0_26_0.py` runs cleanly on the snapshot restore

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)** — no dependencies
- **Phase 2 (Foundational)** — requires Phase 1 complete; BLOCKS all stories
- **Phase 3 (US1)** — requires Phase 2; independent of US2/US3/US4
- **Phase 4 (US2)** — requires Phase 2; independent of US1/US3/US4
- **Phase 5 (US3)** — requires Phase 2; benefits from US2 (archive_topic uses adapter impls — but those are in Phase 2)
- **Phase 6 (US4)** — requires Phase 2; benefits from US1 (journal emissions) and US2 (delivery routing)
- **Phase 7 (Polish)** — requires all desired user stories complete

### Within-phase dependencies

- T004 (events.py) must precede T005 (events tests), T024/T025 (journal emissions)
- T006 (state schema) must precede T007 (state machine) must precede T008 (state-machine tests), T037/T038/T039 (awaiting-state helpers), T045/T046/T047/T048 (lifecycle ops)
- T009 (ABC extension) must precede T010/T011/T012 (adapter impls) must precede T013 (parity tests), T041 (migration topic-create), T048 (delete archive)
- T014 (check_lock + heartbeat) must precede T015 (heartbeat tests), T055 (subprocess loop), T056 (scheduler-side recovery), T057/T058 (orphan paths)
- T016 (delivery header renderer) must precede T017 (header tests) and is consumed by every story's delivery path
- T002 (migration skeleton) must precede T041 (US2 topic-create step), T068 (migration validation)

### Story independence

- US1 is independently testable once Phase 2 is done (journal + HQ silence + `[GET_EVENTS]` with nothing else wired)
- US2 is independently testable once Phase 2 is done (can create+run a task with dedicated topic + markers + pin even without the new lifecycle ops)
- US3 requires state machine (Phase 2) but otherwise independent of US1/US2/US4
- US4 requires state machine + lock primitives (Phase 2) but otherwise independent

### Parallel opportunities

Within Phase 2, the following run in parallel once prerequisites met:
- T005 (events tests) ‖ T008 (state-machine tests) — different files, both depend on their respective impls
- T010/T011/T012 (three adapter impls) — different files
- T013 (parity tests) — after T010/T011/T012
- T015 (heartbeat tests) ‖ T017 (header tests)

Within each user story's phase, tests marked [P] run in parallel before implementation tasks. Implementation tasks are mostly sequential because they share the same files (scheduler.py, continuous.py, lifecycle_macros.py).

Across stories, once Phase 2 completes, US1/US2/US3/US4 can proceed in parallel if staffed with multiple developers.

---

## Parallel Example: Phase 2 foundational adapter impls

```bash
# Three adapter implementations can land in parallel PRs:
Task: "Implement 5 new methods in bot/messaging/telegram.py"     # T010
Task: "Implement 5 new methods in bot/messaging/discord.py"      # T011
Task: "Implement 5 new methods in bot/messaging/slack.py"        # T012

# Parity tests consume all three, so T013 runs after.
```

## Parallel Example: User Story 1

```bash
# All three US1 test files can be written concurrently before implementation:
Task: "tests/test_events_macro.py"      # T018
Task: "tests/test_hq_silence.py"        # T019
Task: "tests/test_hq_fallback.py"       # T020

# Implementation tasks T021–T031 are mostly sequential (same files: scheduler.py, handlers.py).
```

---

## Implementation Strategy

### MVP first (US1 + US2 — both P1)

1. Complete Phase 1: Setup (T001–T003)
2. Complete Phase 2: Foundational (T004–T017) — BLOCKING
3. Complete Phase 3: US1 — HQ silence + journal + `[GET_EVENTS]` (T018–T031)
4. **STOP and validate**: `[GET_EVENTS]` works, HQ is silent. Deploy/demo.
5. Complete Phase 4: US2 — dedicated topic + markers + pin + migration (T032–T042)
6. **STOP and validate**: new tasks land in dedicated topics with state markers; existing tasks migrated. Deploy/demo MVP.

### Incremental delivery of P2

7. Complete Phase 5: US3 — lifecycle contract (T043–T051). Deploy/demo.
8. Complete Phase 6: US4 — heartbeat + drain + backoff (T052–T062). Deploy/demo.
9. Complete Phase 7: Polish (T063–T072). Tag v0.26.0.

### Parallel team strategy

With two developers post-Phase-2:
- Dev A: US1 (journal + HQ silence) → US3 (lifecycle)
- Dev B: US2 (topics + markers + migration) → US4 (heartbeat + drain)
- Both converge on Phase 7 polish together

---

## Notes

- Tests are MANDATORY per Constitution Principle IV; every code task has at least one test task in its phase.
- [P] tasks modify different files — those in the same file are sequential even if logically independent.
- [Story] label enables per-story PR grouping and review traceability.
- Commit after each task or at each checkpoint.
- Every implementation task that modifies scheduler.py or handlers.py MUST run the existing `tests/` regression suite before commit (per-project convention).
- Golden error messages in `contracts/lifecycle-ops.md` are verbatim-testable; do not paraphrase during implementation.
- Migration `v0_26_0.py` is populated across T002 (skeleton) → T041 (US2 topic-create step). T068 validates idempotency.
- Slack WARN logs (T012) are a documented degradation per Constitution Principle I; release notes (T064) must reiterate.
