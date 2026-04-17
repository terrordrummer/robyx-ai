# Feature Specification: Fix Continuous Task Macro Leak

**Feature Branch**: `004-fix-continuous-task-macro`
**Created**: 2026-04-17
**Status**: Draft
**Input**: User description: "Quando andiamo a creare un continuous task attraverso questo create continues, questa macro non funziona. Mi vengono tutte le istruzioni che contengono il comando per creare questo task, che vengono inviate come messaggio all'utente invece di rimanere informazioni interne. L'agente che si è incaricato di creare questo task continuo dovrebbe usarle per concretamente dare le istruzioni al nuovo agente, creare il canale e schedulare il task. Dobbiamo fixare questo problema."

## Context

Robyx supports a "continuous task" capability: a user can ask a workspace or orchestrator agent to set up an iterative, self-driving work program (objective, success criteria, constraints, checkpoint policy, first step). When the setup interview is complete, the agent is supposed to emit a structured macro block that the bot intercepts. The bot then (a) removes the macro from the message that is relayed to the user, (b) creates a dedicated workspace topic/channel, (c) creates a git branch for the work, and (d) schedules and dispatches the first step to the step agent.

Today, the interception is unreliable. In the failure case the user observes the raw macro (including the full program payload) delivered verbatim as a chat message, and no channel, branch, or scheduled step is created. The continuous task never actually starts, and the chat becomes noisy with internal plumbing text.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Setup flow produces a real, running continuous task with no macro leakage (Priority: P1)

A user holds a setup conversation with a workspace or orchestrator agent about an iterative task (e.g., "keep improving the deconvolution module until the benchmark drops below 500 ms"). Once the agent and user agree on the program, the agent emits the continuous-task macro. From the user's point of view, the chat response confirms the task was created (friendly summary — name, new topic reference, branch) and nothing else. Meanwhile, behind the scenes, a new dedicated workspace/topic exists, a git branch exists, and the first step has been scheduled and started.

**Why this priority**: This is the core promise of the feature. When it fails, continuous tasks cannot be created at all via the interview path, and the user is exposed to internal plumbing text that is confusing and unusable.

**Independent Test**: Run the setup interview to completion with any supported platform (Telegram, Discord, Slack). Verify that: the reply message contains no macro tokens or raw JSON program payload; a new topic/channel with the continuous-task indicator exists; a git branch for the task exists; the step agent has been dispatched for step 1; and the task state file is present on disk.

**Acceptance Scenarios**:

1. **Given** a workspace agent that has finished interviewing the user and agreed on a valid program, **When** the agent's response carrying the macro is processed, **Then** the user sees only a friendly confirmation (task name, topic reference, branch name) with no macro tokens or JSON payload, and the topic, branch, and scheduled step all exist.
2. **Given** the orchestrator agent (not a workspace agent) finishes a setup interview and emits the macro, **When** its response is processed, **Then** the same guarantees hold: no leakage, topic created, branch created, first step scheduled.
3. **Given** the setup happens in a collaborative group channel where multiple agents may speak, **When** the designated agent emits the macro, **Then** the macro is intercepted and not echoed to the group chat.

---

### User Story 2 - Macro with malformed payload never leaks to the user (Priority: P1)

An agent emits the macro but the embedded program payload is incomplete or invalid (e.g., missing closing delimiter, malformed JSON, missing required fields, invalid work directory). The user must never see the raw macro or JSON. Instead, the user receives a concise, friendly error message in plain prose explaining that the continuous task could not be created and (where possible) why, without exposing internal tokens or payload structure.

**Why this priority**: A partial or invalid emission is the most common way this bug surfaces today. Even in the failure path, the bot must not dump protocol-level text into the chat.

**Independent Test**: Feed synthetic agent responses that contain each malformed variant (opening tag only, program block only, invalid JSON, JSON with missing keys, work_dir outside workspace). Verify the user-visible output contains no macro tokens and no raw JSON, and that it contains a short human-readable error.

**Acceptance Scenarios**:

1. **Given** an agent response containing the opening macro tag but missing the program block, **When** processed, **Then** the user message contains no macro tokens and includes a short error describing that the continuous task was not created.
2. **Given** an agent response containing a program block but no opening tag, **When** processed, **Then** the program block is not visible to the user.
3. **Given** an agent response with well-formed tags but JSON that fails to parse, **When** processed, **Then** the tags and JSON are both removed and the user sees a short error.
4. **Given** an agent response with tags and parseable JSON but missing required fields (e.g., no `objective`), **When** processed, **Then** tags and payload are removed and the user sees a short error naming what is missing.
5. **Given** an agent response with a `work_dir` that resolves outside the allowed workspace root, **When** processed, **Then** tags and payload are removed and the user sees a short refusal explaining the path was rejected.

---

### User Story 3 - Macro embedded in varied natural-language surroundings is still intercepted (Priority: P2)

Agents compose free-form responses around the macro. They may wrap the block in code fences, add leading or trailing prose, split tags across different whitespace or line-break patterns, emit smart quotes (curly apostrophes produced by some tokenizers), or duplicate the block. The interception layer must tolerate all reasonable variations the LLM produces and still strip the entire block.

**Why this priority**: Even after the core path is fixed, fragile pattern matching will reintroduce the leak the first time an agent produces a slightly different rendering. Robustness against realistic variations is what keeps the fix durable.

**Independent Test**: Run a suite of realistic agent-response fixtures (code-fenced macro, smart quotes around attribute values, extra whitespace, leading narration, multiple macros in one response) through the response processor. Verify each results in zero leaked tokens and the expected number of continuous-task creations.

**Acceptance Scenarios**:

1. **Given** the macro is wrapped in a triple-backtick code block, **When** processed, **Then** the entire fenced block (tags, payload, and fences that exist only around the macro) is removed from the user-visible output.
2. **Given** the macro uses curly quotes around attribute values, **When** processed, **Then** it is still recognized and removed.
3. **Given** the agent emits two macros in a single response, **When** processed, **Then** both are intercepted and executed (or both rejected with a clear error), and neither leaks.
4. **Given** the agent writes prose before or after the macro ("Here's the plan: … [CREATE_CONTINUOUS …] … Let me know if you want changes"), **When** processed, **Then** only the surrounding prose (minus the macro) reaches the user.

---

### Edge Cases

- Streaming output: the macro may arrive split across multiple partial response chunks. Interception must occur on the assembled final response before delivery, not on individual chunks.
- Mixed case or stray whitespace in tag names (e.g., `[create_continuous ...]` or extra newlines between attributes).
- Macro emitted by an agent that is not supposed to create continuous tasks (permissions boundary): the tags must still be stripped, and the user must see a concise refusal instead of the raw block.
- Macro emitted but the target topic/channel creation fails (platform API error, rate limit): the user must see a short error, not the raw macro.
- Duplicate name collision: emitting a macro for an already-existing continuous task name. Tags stripped; user told the name is taken.
- TTS/voice delivery: the chat-visible message and any TTS rendering must both be macro-free.
- Parent-workspace attribution: when emitted from a thread that maps to no workspace, the system has an existing "robyx" fallback — that fallback behavior must be preserved and must not surface to the user.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST detect the continuous-task creation macro in any final agent response and remove it in its entirety from the text that is delivered to the user, across all supported platforms (Telegram, Discord, Slack) and all messaging paths (interactive chat, collaborative group, scheduled delivery).
- **FR-002**: The system MUST, upon detecting a well-formed macro with a valid payload, perform the full set of side effects internally: create the dedicated continuous-task topic/channel, create the associated git branch, persist the task state, and schedule/dispatch the first step — without the agent's original chat response containing the macro tokens or payload.
- **FR-003**: The system MUST replace the removed macro with a concise, human-readable confirmation in the user-visible response that references the created task (task name, topic, branch) and nothing more; protocol tokens, JSON payloads, and internal identifiers beyond those three facts MUST NOT appear.
- **FR-004**: The system MUST, when any part of the macro is detected but cannot be acted upon (missing paired block, malformed JSON, missing required fields, path-traversal rejection, name collision, permission denial, downstream failure), strip every detected token and payload from the user-visible response and substitute a short plain-prose error message.
- **FR-005**: The detection MUST tolerate realistic surface variations in agent output, including but not limited to: leading/trailing prose, wrapping in code fences, additional whitespace or line breaks between attributes, curly/smart quotation marks around attribute values, and multiple macros in one response.
- **FR-006**: The detection MUST be case-insensitive for the tag names and tolerant of both ASCII and typographic punctuation commonly produced by language models in attribute values.
- **FR-007**: The system MUST handle multiple macros in one response by processing each independently: each successful one yields its own side effects and its own confirmation line; each failing one yields its own error line; no raw tokens from any of them leak.
- **FR-008**: When the macro is emitted by an agent that does not have authority to create continuous tasks in the current context, the system MUST still strip all tokens and payload from the user-visible output and surface a short refusal message in their place.
- **FR-009**: Interception MUST occur on the final, fully-assembled agent response prior to user delivery, so that streaming/partial chunks cannot cause fragments of the macro to be shown.
- **FR-010**: The system MUST log, at minimum, each macro detection and its outcome (created, rejected-with-reason, malformed-with-reason) with enough detail for an operator to diagnose failures from logs, without relying on the chat transcript.
- **FR-011**: Any TTS or alternate rendering of the same response MUST be produced from the macro-stripped text, so voice and chat channels stay consistent and leak-free.
- **FR-012**: Existing successful behaviors — continuous-task state creation, scheduler hand-off, `parent_workspace` attribution fallback to `robyx` when the source thread has no mapped workspace, and `work_dir` confinement to the workspace root — MUST be preserved unchanged.

### Key Entities *(include if feature involves data)*

- **Continuous Task Macro**: A structured block that an agent embeds in its free-form reply to request the creation of an autonomous iterative task. Has two paired parts: an opening declaration (task name, work directory) and a program payload (objective, success criteria, constraints, checkpoint policy, first step, optional context). Treated as internal protocol — never user-visible.
- **Agent Response**: The final assembled text produced by an agent turn, which may contain zero or more macros interleaved with prose. Consumed by the response processor before delivery.
- **Continuous Task**: The runtime entity created when a valid macro is processed. Has a name, a parent workspace, a dedicated topic/channel, a git branch, a work directory, and persisted state that drives the scheduler.
- **User-Visible Response**: The text actually delivered to the user on the platform, after macro stripping and confirmation/error substitution. This is the surface where the bug today manifests as a leak.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In end-to-end tests covering the three supported platforms and the three agent roles that can emit the macro (workspace, orchestrator, collaborative), zero test responses contain any fragment of the macro tokens or the program payload in the user-visible output — 100% of runs.
- **SC-002**: For every well-formed macro accepted by the system, the corresponding continuous-task topic, git branch, state file, and scheduled first step are all created within a single user-facing response cycle in at least 99% of runs.
- **SC-003**: For malformed macro variants covered by the test fixture set (missing paired block, bad JSON, missing required fields, disallowed work_dir, name collision), 100% of responses are cleaned of tokens and payload, and 100% contain a short plain-prose error (no JSON, no tag names).
- **SC-004**: For the realistic-variation fixtures (code-fenced, smart quotes, surrounding prose, multiple macros, extra whitespace), interception succeeds in at least 95% of cases, with zero leakage in the remaining cases (i.e., the failure mode, if any, is "not executed" — never "leaked").
- **SC-005**: After the fix ships, user-reported incidents of raw continuous-task macros appearing in chat drop to zero across a 30-day observation window.
- **SC-006**: No regression in already-passing continuous-task tests: the existing scheduler hand-off, parent-workspace attribution, and work_dir-confinement behaviors remain green.

## Assumptions

- The fix is a reliability/robustness correction to an existing feature; the macro grammar and the set of side effects (topic + branch + state + schedule) remain conceptually the same. No new user-facing capability is added.
- "The agent tasked with creating this continuous task" in the user's report refers to whichever agent (workspace, orchestrator, or collaborative) the conversation is happening with — all of them share the same response-processing pipeline, so the fix must apply uniformly.
- Short error messages in the user-visible response are acceptable and preferred over silent failures: users need to understand that their request did not complete.
- Logging destinations and log detail levels already in use are sufficient; this spec does not require new telemetry beyond making detection and outcomes observable.
- Backwards compatibility of the macro grammar with existing agent prompts is preserved: no agent prompt rewrite is required purely to satisfy this fix (though prompt tightening is not forbidden if it reduces leak risk).
- Existing security invariants (work_dir must resolve under the workspace root; reserved names are rejected) stay in force and are only reinforced, not relaxed, by this work.
