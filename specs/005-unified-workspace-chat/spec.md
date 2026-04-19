# Feature Specification: Unified Workspace Chat for Scheduled & Continuous Tasks

**Feature Branch**: `005-unified-workspace-chat`
**Created**: 2026-04-19
**Status**: Draft
**Input**: User description: "Unify continuous, periodic and one-shot task I/O on the parent workspace chat (remove dedicated sub-topic for continuous tasks)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Continuous tasks report in the workspace chat (Priority: P1)

A user defines a continuous autonomous task with the primary workspace agent. Once the plan is confirmed, reports from the scheduler's secondary agent arrive directly in the same workspace chat with a `🔄 [<name>]` prefix. No dedicated sub-topic is opened; the user never has to switch channel to follow or interrupt the work.

**Why this priority**: This is the core UX fix. Today's flow creates a dedicated sub-topic that often ends up empty (race conditions, macro leak), giving the user the impression that the task failed even when it was merely routed away from sight. Removing the sub-topic and keeping the conversation unified restores the "one agent, one chat" mental model.

**Independent Test**: Request a continuous task in a workspace chat, confirm the plan, wait ≤2 scheduler ticks (~2 minutes). Verify that the first step report appears in the same workspace chat with the 🔄 prefix and that no new sub-topic/thread has been created on the platform.

**Acceptance Scenarios**:

1. **Given** an active workspace chat, **When** the user requests a continuous task and confirms the plan, **Then** no dedicated sub-topic is created on the platform and the first step report appears in the workspace chat within 2 scheduler ticks, prefixed with `🔄 [<task-name>]`.
2. **Given** a running continuous task reporting in the workspace chat, **When** the user sends a normal interactive message to the primary agent in the same chat, **Then** the primary responds normally without confusing its reply with a continuous-task report, and the continuous task keeps iterating on schedule.
3. **Given** a continuous task in progress, **When** the scheduler's secondary agent produces output, **Then** the raw macro tokens (`[CREATE_CONTINUOUS ...]`, `[CONTINUOUS_PROGRAM]...[/CONTINUOUS_PROGRAM]`) never appear in the message visible to the user.

---

### User Story 2 - Primary agent manages all task lifecycle from the workspace chat (Priority: P1)

The user controls every scheduled/continuous task — lifecycle, status, stop, pause — by talking naturally to the primary workspace agent in the workspace chat. The primary agent keeps an accurate view of all active tasks in the workspace and answers lifecycle questions with a concise summary or, when a request is ambiguous (multiple matching tasks), asks a clarifying question before acting.

**Why this priority**: Without a single, reliable control point the user cannot trust the system. Interruption today lives in the sub-topic, which breaks the "talk to one agent" model and causes abandoned tasks. This story makes the primary agent the authoritative controller for every task type in the workspace.

**Independent Test**: In a workspace with ≥2 active tasks across types (e.g. a continuous and a periodic), ask the primary agent "lista task" — verify a grouped, icon-marked summary. Then ask "ferma report" when two tasks match — verify the primary asks which one before acting. Then issue an unambiguous stop command — verify the task is stopped and its state reflects that.

**Acceptance Scenarios**:

1. **Given** a workspace with ≥1 active task of any type, **When** the user asks "lista task" (or equivalent natural phrasing), **Then** the primary agent returns a concise summary grouped by task type, each entry prefixed with its icon, status (pending/running/paused/error/completed), and next scheduled run when applicable.
2. **Given** a workspace with multiple active tasks, **When** the user issues an ambiguous stop/pause command (e.g. "ferma report" and two tasks match the word "report"), **Then** the primary agent lists the matching tasks and asks the user to pick one before doing anything.
3. **Given** a workspace with a uniquely identifiable running task, **When** the user issues "stop <name>" or "pausa <name>", **Then** the primary agent applies the action (stop or pause), persists the state change, confirms to the user in chat, and the scheduler ceases to dispatch that task on subsequent ticks (for stop) or resumes it when requested (for pause).
4. **Given** a running task, **When** the user asks "stato <name>", **Then** the primary agent returns objective, current state, last step summary, history length, and any active constraints.

---

### User Story 3 - Visual marker for every scheduled delivery (Priority: P2)

Every message the system delivers into a workspace chat on behalf of a scheduled/continuous task carries a consistent icon marker that identifies the task type and name at a glance. The marker is applied uniformly by the delivery layer (not by individual agent prompts), so it is guaranteed to be present on every scheduled delivery regardless of channel or agent behavior.

**Why this priority**: Once all task reports share the workspace chat, the user needs to tell them apart from conversational content and from each other. A small, consistent visual convention preserves readability without reintroducing channel fragmentation.

**Independent Test**: Trigger one delivery from each task type (continuous, periodic, one-shot, reminder) into the same workspace chat. Verify each delivery begins with the correct icon and task name, and that conversational replies from the primary agent do NOT carry any of these markers.

**Acceptance Scenarios**:

1. **Given** any scheduled/continuous task delivery, **When** the message is posted to the workspace chat, **Then** it is prefixed in the form `<icon> [<task-name>] …` where the icon matches the task type: 🔄 continuous, ⏰ periodic, 📌 one-shot, 🔔 reminder.
2. **Given** a secondary agent whose raw output does not include the marker, **When** the delivery layer posts the message, **Then** the marker is added automatically in a single place before posting.
3. **Given** a conversational reply produced by the primary agent in response to a user message, **When** it is posted to the workspace chat, **Then** no task-delivery marker is prepended.

---

### User Story 4 - Migration of existing continuous tasks (Priority: P2)

All continuous tasks that exist at the time of rollout are migrated so they begin reporting into their parent workspace chat. The legacy `🔄 <name>` sub-topic is closed or deprecated in a best-effort way per platform, and a one-line transition notice is posted into the parent workspace chat so the user understands what happened. Running the migration twice produces no additional changes and no duplicated notices.

**Why this priority**: Without migration, users with pre-existing continuous tasks would experience a mixed state where some tasks still post into old sub-topics while new ones follow the new model — exactly the fragmentation this feature is meant to eliminate.

**Independent Test**: Before migration, confirm an existing continuous task reports in its legacy sub-topic. Run the migration. Verify the parent workspace chat receives the transition notice once, that subsequent scheduled deliveries for that task land in the parent workspace chat with the 🔄 marker, and that the legacy sub-topic is closed or visibly marked as superseded. Re-run the migration: no new notice, no state changes.

**Acceptance Scenarios**:

1. **Given** an existing continuous task pointing at a legacy sub-topic, **When** the migration runs, **Then** its persisted delivery target is repointed to the parent workspace chat and the next scheduled delivery lands there with the 🔄 marker.
2. **Given** a platform that supports closing/archiving sub-topics, **When** the migration runs, **Then** the legacy `🔄 <name>` sub-topic is closed/archived; on platforms without that capability, a final notice is posted in the legacy sub-topic and the system stops delivering there.
3. **Given** a migration already completed for a task, **When** the migration is run again, **Then** no duplicate transition notice is posted in the parent workspace chat and no state is rewritten.
4. **Given** any migration action (per task), **When** it succeeds or fails, **Then** the outcome is logged with the task name and a timestamp.

---

### User Story 5 - Secondary agent shares the primary's workspace knowledge (Priority: P3)

The scheduler's headless secondary agent that executes each continuous step works from the same workspace instructions as the primary agent, augmented by a task-specific detailed plan captured at creation time. The primary agent can read that plan on demand so the user can ask about it without leaving the workspace chat.

**Why this priority**: This is a correctness/consistency safeguard rather than a user-visible flow. It prevents the secondary agent from drifting in behavior from the primary and ensures the primary can answer "what's <name> doing?" accurately. It's important but does not unblock the P1 UX fixes.

**Independent Test**: Create a continuous task with a non-trivial plan. Inspect the artifacts produced at creation: verify both the shared workspace instruction and the task-specific plan are present and readable. When the secondary agent runs a step, verify its behavior is consistent with both artifacts. Ask the primary "dimmi il piano di <name>" and verify it returns the plan content.

**Acceptance Scenarios**:

1. **Given** a newly created continuous task, **When** the scheduler dispatches the first step, **Then** the secondary agent's context includes (a) the workspace agent instructions used by the primary, (b) the task-specific plan captured during setup, and (c) the current state of the task.
2. **Given** a task with an existing plan, **When** the user asks the primary for the plan details, **Then** the primary reads and summarizes the plan in the chat without requiring the user to open any other location.

---

### Edge Cases

- User issues a stop/pause command targeting a task that does not exist or has already completed → primary confirms politely that there is nothing matching to act on, no error state created.
- Migration encounters a continuous task whose state file is corrupted or missing the parent workspace reference → the migration logs an error for that task, skips it, and continues with the rest; the user can query the failed list via the primary.
- A user-facing message happens to contain characters that look like a task marker (e.g., someone types "🔄 [demo]") → the system must not confuse user content with delivery-layer markers; marker semantics are defined only on outbound scheduled deliveries, not on inbound user messages.
- A continuous task fires faster than the user's read speed → markers and task name must be sufficient to distinguish consecutive reports in the chat; user is not required to disambiguate by content alone.
- Platform rate limits or transient failures while posting a scheduled delivery → the delivery is retried within the scheduler's existing retry semantics without losing the marker or producing duplicate user-visible messages.
- Ambiguous request targeting a task name that is a substring of multiple tasks ("stop report" when "daily-report" and "weekly-report" exist) → primary asks which one; never picks silently.
- A workspace hosts both active and completed tasks with the same human-readable name reused across time → list/status commands scope to currently-active tasks by default; the user can still ask for historical ones explicitly.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST NOT create a dedicated sub-topic/thread/channel when a new continuous task is created. Continuous tasks reuse the parent workspace chat as their delivery target.
- **FR-002**: The system MUST persist, for every continuous task, a reference to the parent workspace chat as the authoritative delivery target from the moment the task is created.
- **FR-003**: Every scheduled delivery (continuous, periodic, one-shot, reminder) MUST be posted to the parent workspace chat.
- **FR-004**: Every scheduled delivery MUST be prefixed by a type-specific icon and the task name in the form `<icon> [<task-name>] …`. The icon set is: 🔄 continuous, ⏰ periodic, 📌 one-shot, 🔔 reminder.
- **FR-005**: The icon/task-name prefix MUST be applied by the delivery layer at a single chokepoint, not by the agents producing the payload, so that any agent output is marked consistently.
- **FR-006**: Macro tokens used to declare continuous tasks (`[CREATE_CONTINUOUS ...]`, `[CONTINUOUS_PROGRAM]...[/CONTINUOUS_PROGRAM]`) and any other internal control tokens MUST NOT appear in user-visible messages. Stripping MUST happen uniformly at the final user-visible output chokepoint, across all delivery paths (including interactive, voice/TTS, and platform-specific paths).
- **FR-007**: The primary workspace agent MUST recognize natural-language requests to list tasks in the workspace ("lista task", "che task ci sono", or equivalent) and MUST respond with a concise summary grouped by task type, with each entry showing icon, name, status, and next scheduled run when applicable.
- **FR-008**: The primary workspace agent MUST recognize natural-language status requests for a specific task ("stato <name>", "come va <name>") and MUST respond with objective, current state, last step summary, history length, and any active constraints.
- **FR-009**: The primary workspace agent MUST recognize natural-language stop and pause commands ("ferma <name>", "stop <name>", "pausa <name>") and MUST apply them to the named task after confirming the match.
- **FR-010**: When a lifecycle command (status/stop/pause) is ambiguous — no matching task, or multiple tasks match — the primary agent MUST NOT silently guess. It MUST present matching candidates (or report none found) and ask the user to choose before acting.
- **FR-011**: Stop MUST terminate further scheduled dispatches of the target task and persist the terminal state. Pause MUST halt further dispatches without clearing task state, so the task can later be resumed. Resume MUST be requestable via natural language.
- **FR-012**: The secondary agent that executes a continuous step MUST be provided with the same workspace-level instructions used by the primary, together with the task-specific plan captured at creation time and the current task state.
- **FR-013**: A task-specific plan document MUST be produced at continuous-task creation time and MUST be readable by the primary on demand, so the user can ask about the plan without leaving the workspace chat.
- **FR-014**: A migration routine MUST be provided that, for every pre-existing continuous task, repoints its persisted delivery target to the parent workspace chat, best-effort closes/archives the legacy sub-topic (or posts a final notice where closing is not supported), posts a single transition notice into the parent workspace chat, and logs the outcome.
- **FR-015**: The migration routine MUST be idempotent: re-running it MUST NOT post additional transition notices, MUST NOT alter state for already-migrated tasks, and MUST NOT produce errors on already-migrated inputs.
- **FR-016**: Every lifecycle action invoked by the primary (list/status/stop/pause/resume) and every scheduler-driven delivery MUST be logged with at least task name, task type, action, and timestamp. No state mutation may occur without a corresponding log entry.
- **FR-017**: The system MUST scope task listing, status, and lifecycle commands to the workspace in which they are issued. A user in workspace A MUST NOT see or affect tasks belonging to workspace B through workspace A's chat.
- **FR-018**: Marker semantics MUST apply to outbound scheduled deliveries only. User-authored inbound messages that happen to contain icon/marker-like text MUST NOT be treated as system deliveries.

### Key Entities

- **Workspace**: The user-visible conversation surface (chat or topic within a chat) where the primary agent operates. It has a stable identifier used as the delivery target for every task created from within it.
- **Task**: A scheduled or continuous unit of work owned by a workspace. Has a type (continuous, periodic, one-shot, reminder), a human-readable name unique within its workspace while active, a state (pending/running/paused/error/completed), and — for continuous tasks — an objective, plan, history, and step state.
- **Plan**: A task-specific document produced during continuous-task setup that captures objective, constraints, stop conditions, and step structure. Readable by the primary on demand; provided to the secondary agent at dispatch.
- **Delivery**: A single outbound message posted to the parent workspace chat on behalf of a task, carrying the appropriate marker and the sanitized message body.
- **Scheduler Queue**: The centralized list of tasks awaiting dispatch, from which the scheduler selects due entries each tick. Shared across all task types.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: When a user creates a new continuous task, the first step report lands in the parent workspace chat with the `🔄 [<name>]` marker within 2 scheduler ticks in 95% of cases, with zero sub-topics created on the platform.
- **SC-002**: Zero user-visible messages contain raw macro tokens (`[CREATE_CONTINUOUS ...]`, `[CONTINUOUS_PROGRAM]...[/CONTINUOUS_PROGRAM]`) across any delivery path.
- **SC-003**: When the user issues a lifecycle command (list/status/stop/pause) in a workspace, the primary agent responds with a correctly-scoped, icon-marked summary or action confirmation within 5 seconds in 95% of cases.
- **SC-004**: When a lifecycle command is ambiguous, the primary agent asks a clarifying question instead of guessing in 100% of cases.
- **SC-005**: After running the migration once, 100% of pre-existing continuous tasks deliver their next scheduled step into their parent workspace chat with the 🔄 marker, and each migrated task has exactly one transition notice in the parent workspace chat.
- **SC-006**: Re-running the migration produces zero additional transition notices and zero state changes (idempotency verified end-to-end).
- **SC-007**: Every scheduler-driven delivery and every primary lifecycle action produces a log record with task name, task type, action, and timestamp; log coverage is 100%.
- **SC-008**: Over a one-week observation window after rollout, there are zero reports of a continuous task that appears "silent" because its reports landed somewhere the user did not see.

## Assumptions

- The existing unified 60-second scheduler and single queue model remain in place; this feature does not redesign scheduling cadence or queue format.
- Continuous-task state schema changes are limited to repointing the delivery target to the parent workspace chat. Other state fields (objective, history, step state) are preserved as-is.
- Each workspace has a single canonical chat identifier that can serve as the delivery target for every task created from within it. Where platforms support threads/forum topics, the workspace's own thread is used — not a new one.
- The primary workspace agent has access to the scheduler queue and to per-task state to answer list/status requests without additional user-side configuration.
- Git-branch-per-continuous-task behavior is preserved and unchanged.
- The closing/archiving of legacy sub-topics is best-effort per platform. Where a platform does not expose an archive/close capability, the system falls back to posting a final notice in the legacy sub-topic and silently discontinuing its use.
- A continuous task's human-readable name is unique within its workspace while active; reuse across time is allowed only after the previous instance has completed or been stopped.
- Authorization: any user with access to the workspace chat can issue lifecycle commands. Multi-user permission controls are out of scope for this feature.
- Observability is served by the same logging pipeline already in use; no new logging infrastructure is introduced.
