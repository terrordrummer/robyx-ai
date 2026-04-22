# Feature Specification: Continuous-Task Observability & Lifecycle Robustness

**Feature Branch**: `006-continuous-task-robustness`
**Created**: 2026-04-22
**Status**: Draft
**Input**: User description: "Continuous-Task Observability & Lifecycle Robustness — address 9 interconnected fragilities in the continuous-task subsystem surfaced by real incidents (zeus-rd-172, zeus-research, zeus-engine, align-research): unified topic mixing, silent awaiting-input state, generic delivery format, dispatch-noise in HQ, name_taken after stop, workspace-close orphans, stale-lock starvation, orphan detection spam, resume of vanished task."

## Clarifications

### Session 2026-04-22

- Q: With what mechanism does the HQ orchestrator query the event journal? → A: Macro pattern `[GET_EVENTS since="…" task="…" type="…"]` emitted by the orchestrator LLM, intercepted by the handler, result injected back as a system-level context message for the same turn. Consistent with the existing `[GET_PLAN]` / `[NOTIFY_HQ]` macro family.
- Q: What happens to the dedicated topic when a continuous task is deleted? → A: Archive + rename — the topic is renamed to `[Archived] <name>`, closed to new messages, and kept readable as a permanent record of the autonomous work. The task name is freed for immediate reuse. No hard-delete.
- Q: Under what conditions, if any, does HQ receive an automatic message? → A: Last-resort only. HQ receives a single automatic message **if and only if** (i) the task's dedicated topic is unreachable (e.g. deleted manually by the user, or platform API failure sustained beyond retry window) AND (ii) the event is user-actionable (awaiting input, terminal error, task death). Routine events (dispatches, step completions, lock recoveries, state transitions, rate-limit enter/exit) never surface in HQ automatically — the `[GET_EVENTS]` macro is the sole pull-based path.
- Q: Is the drain-on-close timeout fixed or configurable? → A: Per-task configurable field `drain_timeout_seconds` stored in task state, default 3600 (60 minutes). Chosen to sit above the P95 of observed long step durations (~28 min on zeus-rd-172) while remaining bounded enough to prevent indefinite waits on a stuck subprocess. Researchers with atypical step profiles can tune per task at create time.
- Q: Does the event journal cover only continuous tasks or all task types? → A: Continuous-only for the MVP, but the journal record schema MUST be task-type-agnostic (carries a `task_type` field from day one), and the scheduler MUST expose stubbed hook points for periodic, one-shot, and reminder tasks. Extending coverage later is an additive change — no schema migration, no query-contract change. This avoids future lock-in for ~zero marginal cost.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Silent scheduler, pull-based event history (Priority: P1)

The user is the orchestrator (a human interacting with the principal agent in the main "HQ" topic of the messaging platform). When continuous tasks are running, routine scheduler activity — dispatching a new step every few minutes, recovering a lock, detecting an orphan — must not generate any user-facing notification in HQ. Only events the user needs to see or act upon should produce a visible message, and those must land in the relevant task's own space, not in HQ. At any time, the user can ask the HQ orchestrator "what happened in the last N hours?" and get an accurate, ranked summary of meaningful events across all tasks.

**Why this priority**: The user explicitly identified continuous scheduler-driven noise in HQ as the most annoying part of the current system ("mi arriva una notifica nell'head quarters … ogni minuto, questo riempie la chat in maniera oscena"). Without this fix, all other improvements are buried under notifications. The pull-based history is the contract that makes silence safe: the user can always reconstruct activity on demand.

**Independent Test**: Start two concurrent continuous tasks (e.g. a fast-iterating research loop and a long autonomous one). Over a 2-hour window, verify that HQ receives zero push messages from scheduler activity (dispatches, lock recoveries, orphan detections, state transitions). Then ask the orchestrator "what has happened in the last 2 hours?" and verify it produces a chronologically ordered summary including dispatches, step completions, state transitions, errors, and recoveries for both tasks.

**Acceptance Scenarios**:

1. **Given** two continuous tasks running with ~60-second scheduler ticks, **When** the scheduler dispatches steps for 120 minutes, **Then** HQ receives zero automatic messages about those dispatches.
2. **Given** a continuous task crashes silently and the scheduler detects the orphan, **When** the orphan is marked failed, **Then** no warning surfaces in HQ; the event is recorded in the journal and a single user-facing incident message appears only in the task's own topic.
3. **Given** the user asks the HQ orchestrator "what happened since 14:00?", **When** the orchestrator answers, **Then** the response lists every meaningful event (dispatches, completions, transitions, errors, stops, resumes) with timestamp, task name, and outcome, drawn from the event journal, for all active and recently active tasks.
4. **Given** a continuous task completes a step that produced no substantive output (silent run), **When** the scheduler records the event, **Then** the event is in the journal but no chat message is posted anywhere.

---

### User Story 2 - Dedicated, state-aware topic per continuous task (Priority: P1)

Each continuous task lives in its own dedicated topic in the messaging platform, distinct from the parent workspace's conversation topic. Step output, state transitions, pauses, errors, and closure notices all appear only in that topic. The topic's title carries a live state marker — running, awaiting input, paused, rate-limited, completed, error — so the user sees the task's current state from the topic list without opening it. When a task transitions to "awaiting input" (the agent needs a user decision to proceed), a structured, pinned message appears in the topic with the question and a clear call-to-action; after 24 hours of continued silence, one (and only one) reminder is posted in the same topic. When the user answers, the task resumes and the reminder / pin are cleared.

**Why this priority**: The user explicitly stated that mixing continuous-task messages with normal agent conversation caused confusion, and that not realizing a task had silently paused — and not knowing what was expected of them — was a major source of frustration. Both problems share the same root (no dedicated channel, no state visibility) and must be solved together for the fix to feel complete.

**Independent Test**: Create a new continuous task from a parent workspace. Verify a new dedicated topic appears with a recognizable "[Continuous]" prefix and a state marker in its title. Execute a step that ends in "awaiting input". Verify: (a) nothing new appears in the parent workspace topic, (b) a structured, pinned message appears in the dedicated topic with the question and guidance, (c) the topic title now carries the paused marker, (d) after 24h of silence a single reminder is posted, (e) replying in the dedicated topic clears the pin, restores the running marker, and advances the task.

**Acceptance Scenarios**:

1. **Given** a user issues a create-continuous macro from their workspace, **When** creation succeeds, **Then** a new dedicated topic is created with a name including the "[Continuous]" prefix and the task's display name, and all subsequent step deliveries land in that topic — never in the parent workspace topic.
2. **Given** a continuous step completes in "awaiting input" state with a pending question, **When** delivery happens, **Then** the message carries a visible "awaiting input" state marker, a step counter, a timestamp header, the agent's question quoted verbatim, and a brief instruction on how to respond; the message is pinned; the topic title is updated with a paused marker.
3. **Given** a task has been in "awaiting input" for 24 hours with no user reply, **When** the 24h mark is crossed, **Then** exactly one reminder is posted in the same topic referencing the original pinned question.
4. **Given** the user replies to a paused task in its dedicated topic, **When** the scheduler resumes it, **Then** the pinned message is unpinned, the topic title's state marker returns to running, and the next step runs.
5. **Given** every continuous step delivery, **When** rendered, **Then** the message carries a structured header of the form "[icon] [name] · Step N · STATE · HH:MM" with STATE drawn from the set { running, awaiting input, rate-limited until HH:MM, completed, error, workspace closed }, optionally followed by the next planned step when known.
6. **Given** a task transitions to completed, error, or rate-limited, **When** the state changes, **Then** the topic title's state marker is updated accordingly so the user can see it from the topic list without opening the topic.

---

### User Story 3 - Unambiguous lifecycle: stop, resume, delete, complete (Priority: P2)

The user can manage a continuous task's lifecycle with three distinct, documented operations that behave predictably: **stop** (halt dispatches, keep state/history/topic; task is fully resumable later), **complete** (terminal success; no more dispatches, state/history/topic preserved as record), **delete** (purge everything and free the name for reuse). Stop and complete keep the name reserved as a historical record; delete frees it. Resume on a stopped or awaiting-input task picks up seamlessly; resume on a deleted/missing task surfaces a clear, actionable message telling the user how to recreate it. Creating a task with a name that is still reserved produces a user-friendly error that spells out which of the three lifecycle operations is needed to reuse that name.

**Why this priority**: The user hit `name_taken` after stopping a task and tried to recreate it, with no clear path forward. This class of confusion can block all subsequent work on a task. Second priority because it is a contract/UX fix rather than the core observability pain, but still user-visible and blocking when encountered.

**Independent Test**: Create a task, stop it, verify it remains in the task list as "stopped" and is resumable. Resume it, verify it picks up from the last state. Complete it, verify it stays as "completed" in history but cannot be dispatched. Try to create a new task with the same name — verify the error message explicitly tells you to delete the existing task first. Delete it, verify the name is now free and a fresh create succeeds. Separately, delete a task and then attempt to resume it — verify the error message clearly tells you the task no longer exists and how to recreate it.

**Acceptance Scenarios**:

1. **Given** a running continuous task, **When** the user issues stop, **Then** no further steps are dispatched; state, history, and the dedicated topic are preserved; the task appears in listings as "stopped" and is resumable.
2. **Given** a stopped task, **When** the user issues resume, **Then** the scheduler picks up dispatching from the preserved state without losing any history.
3. **Given** a terminal-complete task, **When** the user queries status, **Then** it appears as "completed"; no dispatches happen; history and topic remain as a permanent record.
4. **Given** a task in any state, **When** the user issues delete, **Then** state, agent definition, queue entries, and the dedicated topic are all removed or archived; the name is immediately available for new tasks.
5. **Given** a task exists in any non-deleted state (stopped, completed, running, awaiting input), **When** the user attempts to create a new task with the same name, **Then** the error message explicitly names the existing task's state and tells the user to either delete it or pick a different name.
6. **Given** a task has been deleted, **When** the user attempts to resume by name, **Then** the error message says the task no longer exists and suggests either recreating it or consulting the event journal for its history.

---

### User Story 4 - Robust execution: no stale locks, no lost output, no orphan spam (Priority: P2)

Continuous tasks make forward progress even when things go wrong at the infrastructure layer. If a step's subprocess is killed, crashes, or the bot itself goes down, the task recovers automatically within minutes of the bot being healthy again — it never stays blocked for days waiting for a human to manually clean up a stale lock. If a workspace is closed while a step is running, that step's output is not lost: either it is delivered to the task's own topic with a clear "workspace closed mid-step" marker, or it is archived with a pointer the user can retrieve later. If the scheduler repeatedly detects that a task has crashed, it stops spamming warnings after a small number of detections and instead raises a single, clear incident in the task's topic and its event journal entry.

**Why this priority**: The user observed 8+ days of silent blockage on one task (stale lock), orphan-warning spam every 30 minutes during another, and output vanishing when a workspace closed mid-step. Each of these degrades trust in the autonomous loop. Priority P2 because they are lower-frequency than the noise/topic problems but equally damaging when they do occur.

**Independent Test**: (a) Start a continuous task, send SIGKILL to its step subprocess, verify the scheduler recovers and dispatches a fresh step within one heartbeat window without a bot restart. (b) Start a continuous task, close its parent workspace while a step is running, verify the step's output either arrives in the task's dedicated topic with a "workspace closed mid-step" marker or is clearly referenced in an archive message. (c) Simulate a reproducible subprocess crash on every dispatch; verify that after 3 consecutive orphan detections, no further scheduler warnings are logged for that task, and a single incident message is posted in the task's topic and recorded in the journal with enough context to diagnose.

**Acceptance Scenarios**:

1. **Given** a running continuous step whose subprocess is killed by an external signal, **When** the lock has not been heartbeat-refreshed within the stale threshold, **Then** the scheduler reclaims the lock automatically on the next cycle and dispatches the next step; no bot restart is required.
2. **Given** the bot is down for several days and a lock remains from the last run, **When** the bot comes back up, **Then** stale locks older than the threshold are cleaned during the normal scheduler cycle (not just at startup) and dispatches resume normally.
3. **Given** a continuous step is mid-run and the user closes the parent workspace, **When** closure is processed, **Then** the running step is allowed to finish (within a bounded drain window) and its output is delivered to the task's dedicated topic with a "workspace closed mid-step" marker, or — if the topic is being deleted as part of closure — archived and referenced by a single archive message.
4. **Given** a continuous task whose steps repeatedly crash without producing output, **When** the scheduler has detected the same orphan condition 3 times in a row, **Then** no further routine warnings are logged, a single incident message is posted in the task's dedicated topic with timestamp and minimal diagnostic (last exit code / last output tail / lock timestamps), and a matching entry is written to the event journal.
5. **Given** a continuous task in "rate-limited" state with a recovery timestamp, **When** the recovery timestamp passes, **Then** the scheduler resumes it on the next cycle without user intervention and the topic title's state marker updates accordingly.

---

### Edge Cases

- **Platform lacks topic primitives** (e.g. a messaging platform without first-class topics/threads): the dedicated-topic requirement degrades to a "prefixed conversation" fallback; state markers use inline text tags instead of topic titles; pinning is best-effort.
- **Topic deletion between sessions**: if a user manually deletes a dedicated continuous-task topic, the next scheduler tick detects detachment and attempts silent recreation. If recreation succeeds, the task continues without any HQ message. If recreation fails (platform refusal, permissions, sustained API failure), the task is marked "detached — action required" in the journal. Per FR-002a, an HQ last-resort message is posted only if the next pending event is user-actionable (awaiting input, terminal error, task death); pure routine events remain silent even in the detached state — the user discovers them via `[GET_EVENTS]`.
- **Event journal grows unbounded**: journal rotation/compaction runs on a cadence (hourly or size-based) and keeps at minimum a 7-day rolling window queryable; older entries are archived but remain reachable by explicit request.
- **Two users hitting stop simultaneously**: the second stop is a no-op with a user-visible "already stopped" message.
- **Create collision in race**: two concurrent creates with the same name — one wins; the loser gets a deterministic `name_taken` message identifying the winning task's state.
- **Awaiting-input reminder when topic is muted**: reminder is still posted; the user controls whether they receive a push notification via their own platform settings.
- **Step finishes during drain-on-close with an `awaiting_question`**: the drain delivery includes both the "workspace closed" marker and the pending question; the task's state becomes `stopped` (not `awaiting-input`) because its container is gone.
- **Clock skew or monotonic-time assumption breaks**: lock heartbeat uses a monotonic-plus-wallclock pair; stale detection favors the conservative interpretation (assume stale rather than alive) to avoid deadlocks.

## Requirements *(mandatory)*

### Functional Requirements

#### Observability — event journal and HQ silence

- **FR-001**: The system MUST record every continuous-task lifecycle event (creation, dispatch, step-start, step-complete, state transition, stop, resume, complete, delete, error, orphan incident, rate-limit enter/exit, lock recovery, drain-on-close) to an append-only event journal with, at minimum: ISO-8601 timestamp, task name, task type, event type, outcome, and a small structured payload. The record schema MUST be task-type-agnostic from day one — the `task_type` field is required on every entry and the set of valid event types MUST include entries meaningful for continuous, periodic, one-shot, and reminder tasks — so that later extension to non-continuous tasks is an additive change with zero schema migration. The scheduler dispatch/completion paths for periodic, one-shot, and reminder tasks MUST expose stubbed hook points (no-op for MVP) ready to journal their events without further refactor.
- **FR-002**: The system MUST NOT push any message into HQ as a direct side-effect of scheduler dispatch activity, lock maintenance, orphan detection, or routine state transitions. The sole exception is the last-resort surface defined in FR-002a.
- **FR-002a**: The system MUST post exactly one automatic message to HQ when, and only when, BOTH of the following conditions hold simultaneously: (i) the task's dedicated topic is unreachable (manually deleted by the user, or platform API failure that persists beyond the configured retry window), AND (ii) the pending event is user-actionable — specifically one of { awaiting-input transition, terminal error, task death (lock starvation incident after orphan backoff) }. The HQ message MUST clearly identify the affected task, the reason the dedicated topic is unreachable, and the user-actionable event; it MUST NOT be used for routine dispatches, step completions, lock recoveries, rate-limit transitions, or any other non-actionable event. Once posted, a suppression flag on the task's state MUST prevent duplicate HQ messages until the topic becomes reachable again or the task is reset.
- **FR-003**: The orchestrator agent in HQ MUST be able to query the event journal on demand by emitting a macro token of the form `[GET_EVENTS since="…" task="…" type="…"]` (with `task` and `type` optional). The handler MUST intercept this macro, execute the query, and inject the chronologically ordered result back into the orchestrator's context as a system-level message for the same turn. The macro pattern MUST follow the same lifecycle conventions as the existing `[GET_PLAN]` / `[NOTIFY_HQ]` family (strip from user-facing output; log invocations; tolerate malformed attributes with a graceful error injected back).
- **FR-004**: The event journal MUST be durable across bot restarts and machine moves (file-based, checked-in or backed up along with existing state), and support at least 7 days of rolling history queryable by default.
- **FR-005**: The system MUST support a silent-step convention: when a step's agent declares the output as informational-only, the event is journaled but no chat message is posted.

#### Dedicated topic, state markers, awaiting-input visibility

- **FR-006**: When a continuous task is created, the system MUST create a dedicated topic in the messaging platform (when supported) whose name includes a "[Continuous]" prefix and the task's display name; all subsequent deliveries for that task MUST target this topic.
- **FR-007**: The system MUST NOT deliver continuous-task step output into the parent workspace's conversation topic under normal operation.
- **FR-008**: Every delivery message for a continuous task MUST carry a structured header containing the task type icon, task name, step counter, current state, and an HH:MM timestamp, in a consistent, parseable format.
- **FR-009**: The system MUST maintain a visible state marker in the dedicated topic's title — drawn from { running, awaiting input, paused, rate-limited, completed, error, workspace closed } — and update the title whenever the task's state changes.
- **FR-010**: On transition to "awaiting input" with a pending question, the system MUST post a structured message containing the question verbatim and a clear call-to-action, pin that message in the dedicated topic, and update the topic title with the paused marker.
- **FR-011**: If a task remains in "awaiting input" continuously for 24 hours, the system MUST post exactly one reminder message in the same dedicated topic referencing the original pinned question; no further automatic reminders are posted for the same awaiting episode.
- **FR-012**: When the user replies to a paused task in its dedicated topic and the task resumes, the system MUST unpin the awaiting message, clear any awaiting-specific markers from the topic title, and restore the running-state marker.
- **FR-013**: The system MUST provide a platform-adapter capability to set/edit topic titles and pin/unpin messages; when a platform lacks these primitives, the adapter MUST degrade gracefully (inline markers, best-effort pin equivalents) without blocking feature delivery.

#### Lifecycle contract

- **FR-014**: The system MUST expose three distinct continuous-task lifecycle operations — stop, complete, delete — with the semantics: stop halts dispatches and preserves all state/history/topic (resumable); complete marks terminal success and preserves all state/history/topic (not resumable, name stays reserved); delete purges state, agent definition, and queue entries, archives the dedicated topic (renamed to `[Archived] <name>` and closed to new messages — never hard-deleted from the platform so the autonomous-work history remains readable), and frees the name for reuse.
- **FR-015**: The system MUST expose a resume operation that takes a stopped or awaiting-input task back to the running state from its preserved position without loss of step history.
- **FR-016**: When a create operation is attempted with a name that is still reserved (stopped, completed, running, awaiting-input), the system MUST return a user-visible error that identifies the existing task's state and tells the user how to reuse the name (delete, or pick another name).
- **FR-017**: When a resume operation targets a task that no longer exists, the system MUST return a user-visible error stating that the task is gone and suggesting either recreating it or consulting the event journal.
- **FR-018**: Stop, complete, and delete MUST each write a distinct event to the journal with enough context (state at the moment of the operation) for the journal to reconstruct the lifecycle.

#### Robust execution

- **FR-019**: Every running step's lock MUST carry a heartbeat timestamp that is refreshed at regular intervals (at most every 60 seconds) for the duration of the step's subprocess.
- **FR-020**: The scheduler MUST treat a lock as stale when its heartbeat timestamp is older than a configured threshold (default: 5 minutes) and MUST reclaim stale locks during its normal cycle — not exclusively at bot startup.
- **FR-021**: When the parent workspace of a continuous task is closed while a step is running, the system MUST allow the step to finish within a per-task drain window (configurable via a `drain_timeout_seconds` field in the task state; default: 3600 seconds = 60 minutes) and deliver its output with a "workspace closed mid-step" marker to the task's dedicated topic. If the task is concurrently being deleted (its topic becomes archived and closed to new messages mid-drain), the final step output MUST instead be recorded as a full-body event in the journal, and a single "drain output recorded in journal — see archived topic or `[GET_EVENTS]`" reference message MUST be posted to the archived topic as its final line before closure where platform primitives permit; if archival has already completed, the reference message is omitted (journal entry is sufficient). If the drain window elapses without the step completing, the subprocess is terminated, a "drain timeout exceeded" event is written to the journal, and the user is notified via the task's dedicated topic (or via the FR-002a last-resort HQ surface if the topic is unreachable) with enough context to diagnose the stuck step.
- **FR-022**: When the scheduler detects that the same task has been in "orphan" condition (state running, no heartbeat) for 3 consecutive cycles, it MUST stop emitting routine warnings for that task, post a single incident message to the task's dedicated topic containing a minimal diagnostic payload (last exit code, last output tail, lock timestamps), record a matching incident entry in the event journal, and set the task state to "error".
- **FR-023**: A task in "rate-limited" state MUST automatically return to dispatching on the first scheduler cycle after its recovery timestamp passes, with the topic title's state marker updated at the transition.

#### Migration

- **FR-024**: The system MUST ship a one-time migration that: (a) creates dedicated topics for all existing continuous tasks that currently share their parent workspace topic, (b) initializes the event journal with synthetic "migration" entries pointing to prior state, (c) sets initial topic-title state markers consistent with each task's current state.
- **FR-025**: The migration MUST be idempotent and leave the system in a consistent state even if interrupted mid-run.

### Key Entities *(include if feature involves data)*

- **Continuous Task**: a named autonomous unit with a lifecycle state (running, awaiting input, paused/stopped, rate-limited, completed, error, workspace-closed, deleted), a parent workspace, a dedicated topic reference, a program definition, and a history of steps.
- **Event Journal Entry**: an append-only record of a single lifecycle or execution event, carrying a timestamp, task name, event type, outcome, and a small structured payload; queryable by time window, task, and type.
- **Step**: a single iteration of a continuous task's program; has a number, start/end timestamps, outcome, and optional output body.
- **Lock Heartbeat**: a periodically refreshed timestamp attached to a running step, used by the scheduler to distinguish live subprocesses from crashed ones.
- **Dedicated Topic**: a messaging-platform topic (or equivalent) owned by a single continuous task; hosts all task-specific deliveries, pinned awaiting-input messages, and state-reflecting title updates.
- **Awaiting-Input Pin**: the single pinned message in a dedicated topic that carries the currently pending question; pinned on transition to awaiting input, unpinned on resume.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With three concurrent continuous tasks running for 2 hours under normal conditions (all dedicated topics reachable, no user-actionable incidents requiring the last-resort surface), HQ receives zero automatic scheduler-related messages.
- **SC-001a**: Under the exception case defined by FR-002a (topic unreachable AND user-actionable event pending), HQ receives exactly one automatic message per affected task per unreachable episode — no duplicates, no routine events leaking, no messages before both conditions are satisfied. Validated across 5 simulated episodes covering awaiting-input, terminal error, and task-death event types.
- **SC-002**: When the user asks the HQ orchestrator "what happened in the last N hours?" for any N between 1 and 24, the response includes every journaled event in that window (100% coverage of lifecycle events, dispatches, completions, state transitions, errors) across all tasks active in that window, in chronological order.
- **SC-003**: 100% of continuous-task step deliveries land only in the task's dedicated topic; the parent workspace topic receives zero continuous-task step messages.
- **SC-004**: Every delivery message in a dedicated topic carries a header conformant to the specified format (parseable by a single regex), validated on every message for 100 consecutive deliveries.
- **SC-005**: On every transition to "awaiting input", a pinned message appears in the dedicated topic within 10 seconds and the topic title carries the paused marker.
- **SC-006**: An awaiting-input reminder is posted exactly once per awaiting episode after 24 ± 1 hours of continuous silence; zero duplicate reminders are observed across 10 simulated awaiting episodes.
- **SC-007**: Creating a new task with an already-reserved name produces an error message that explicitly names the existing task's state and the action required to reuse the name; validated across all four non-deleted reserved states.
- **SC-008**: Resuming a deleted task produces an error message that tells the user the task no longer exists and points them to either recreation or journal inspection; validated in isolation without requiring external context.
- **SC-009**: A step subprocess killed externally (SIGKILL) is detected as stale and its task dispatches a fresh step within 6 minutes (1 heartbeat interval + 1 stale threshold + 1 scheduler cycle), without any bot restart or manual intervention.
- **SC-010**: A stale lock older than the threshold is reclaimed on a normal scheduler cycle with no bot restart required; the task's next dispatch succeeds.
- **SC-011**: Closing a parent workspace during a running step results in exactly one user-visible message per task about the closure (either the drained step's output with the "workspace closed mid-step" marker or a single archive-reference message); zero in-flight outputs are silently lost.
- **SC-012**: A reproducible-crash task produces exactly one incident message in its dedicated topic after 3 consecutive orphan detections; zero routine warning messages surface in HQ for that task; the event journal contains one matching incident entry.
- **SC-013**: The migration runs to completion on a system with ≥3 existing continuous tasks, creates dedicated topics for each, seeds journal entries, and leaves task states unchanged (no dispatches lost, no state regressions).
- **SC-014**: Across a 7-day live run with ≥2 continuous tasks executing steps, user-reported noise in HQ measured by subjective rating ("how disruptive were continuous-task notifications in HQ?") is "not at all" or equivalent.
- **SC-015**: At any point during the 7-day live run, the event journal can be queried for the full window and returns all events that actually occurred (verified by cross-checking against bot.log dispatch entries).

## Assumptions

- Telegram is the primary target platform for topic/pin/title operations; Discord and Slack adapters will receive degraded implementations (inline markers, best-effort equivalents) that do not block feature delivery.
- The existing `data/` directory layout (state files, queue, locks) remains the canonical on-disk representation; the event journal is added alongside (e.g. a JSON-Lines file) rather than replacing anything.
- The principal orchestrator agent in HQ is a conversational Claude agent capable of invoking a journal-query tool or macro; exact integration mechanism (tool vs macro vs slash-command) is an implementation detail for the plan phase.
- One user per deployment; multi-user conflict resolution on the same continuous task is out of scope.
- Existing research-methodology behaviors of continuous tasks (auto-revert on envelope breach, guardrail gates, kill-switch iterations) are correct as-is and must not be altered by this feature.
- Claude Code session-resume internals (session_id plumbing, --resume, etc.) are out of scope.
- One-shot and periodic task paths are out of scope for user-facing behavior in the MVP (no new topic model, no pinned awaiting flow, no lifecycle ops re-contract). However, the event journal schema MUST be task-type-agnostic from day one (per FR-001) and hook points for their dispatch/completion events MUST be stubbed in the scheduler — extending journal coverage to them later is additive, not a new migration.
- The messaging platform's topic-list ordering and unread badges respect the per-topic notification state; the user controls muting per topic through their own client.
- The scheduler continues to run on a ~60-second cycle; tighter latency is not a goal.
- 7-day rolling event-journal retention is sufficient; longer-term archival can be added later without breaking the query contract.
