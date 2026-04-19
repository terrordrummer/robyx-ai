# Phase 0 Research — Unified Workspace Chat

## R1. Single chokepoint for scheduled delivery marker + macro strip

**Decision**: Apply the type-specific icon marker and the final macro strip inside `bot/scheduled_delivery.py::deliver_task_output()` (specifically, the existing `_clean_result_text()` + `_render_result_message()` helpers). Extend `_render_result_message()` to prepend `"<icon> [<task-name>]\n"` derived from the task type read from the queue entry.

**Rationale**: `deliver_task_output()` is already the sole entry point for scheduled-task output (continuous, one-shot, periodic). The helper `_clean_result_text()` already strips `[STATUS …]` and continuous macros — extending it is localized and inherits existing test coverage. Agent-side prompting to produce a marker is brittle (agent can forget); delivery-side is deterministic.

**Alternatives considered**:
- Mark in agent prompts — rejected: agent drift risk, no guarantee.
- Mark in `platform.send_message()` adapters — rejected: would couple platform layer to task semantics; violates single responsibility.

## R2. Reminder delivery marker path

**Decision**: Reminders are sent by `bot/scheduler.py::_dispatch_reminders()` directly through `platform.send_message(...)` (no LLM, no `deliver_task_output`). Add a small helper `format_delivery_message(task_type, task_name, body)` in `scheduled_delivery.py` that both paths call. Reminders get the `🔔` marker there.

**Rationale**: Keeps the marker logic in one module (`scheduled_delivery.py`) and testable without platform fakes.

**Alternatives considered**:
- Inline the marker in `_dispatch_reminders()` — rejected: duplicates the format rule; two places to keep in sync.

## R3. Interactive path macro strip parity

**Decision**: The final strip for the primary agent's interactive response happens in `bot/ai_invoke.py` at the point the response text is handed to `handlers.py` for sending. The existing `strip_continuous_macros_for_log()` (from `bot/continuous_macro.py`) is already called on the scheduled path; we introduce `strip_control_tokens_for_user()` as the canonical user-facing scrub, used by (a) interactive path in `ai_invoke.py` and (b) the scheduled path in `scheduled_delivery.py`.

**Rationale**: FR-006 requires strip on ALL user-visible paths. Spec 004 P1 observed that the strip was missing on interactive and TTS paths. A single named helper used at both chokepoints closes the gap and makes missing calls easy to spot in code review.

**Alternatives considered**:
- Strip at platform-adapter level — rejected: per-platform duplication, high risk of missing a new adapter.

## R4. Primary-agent lifecycle intents → server-processed macros

**Decision**: Introduce five new system-consumed macros emitted by the primary agent, processed server-side before the response is sent to the user:
- `[LIST_TASKS]`
- `[TASK_STATUS name="…"]`
- `[STOP_TASK name="…"]`
- `[PAUSE_TASK name="…"]`
- `[RESUME_TASK name="…"]`

Handler resides in a new module `bot/lifecycle_macros.py`, invoked from the same chokepoint that processes `CREATE_CONTINUOUS` today (`bot/handlers.py` pre-delivery). The handler queries `queue.json` + `data/continuous/*/state.json`, filters by current workspace (`chat_id`+`thread_id`), applies the action, and substitutes the macro with a rendered text response (markdown, icon-prefixed summary) that then goes through the normal final-output chokepoint.

**Rationale**: This mirrors the existing `CREATE_CONTINUOUS` pattern (agent declares intent, server executes, response is materialized and stripped before user sees it). Keeps intent recognition at the LLM (flexible natural language), keeps actions deterministic.

**Alternatives considered**:
- LLM tool/function calling — rejected: existing codebase uses text macros; introducing tool-calling doubles the surface area and constrains AI-backend choice (Principle I applies: Claude/Codex/OpenCode must all work). Text macros are backend-agnostic.
- Slash commands — rejected: violates Principle II (chat-first, natural language preferred) and breaks Italian/English parity because slash commands in Telegram have restrictive grammar.

## R5. Ambiguity resolution for lifecycle commands

**Decision**: The lifecycle macro handler does the resolution; the primary agent does NOT pre-filter candidates. If `name` matches zero active tasks → handler returns "nessun task attivo corrisponde a …". If it matches exactly one → apply and confirm. If it matches ≥2 → handler renders a numbered list with icons and returns a "scegli quale" text; the primary agent treats the user's follow-up as the disambiguation answer and re-emits the macro with the resolved name.

**Rationale**: Authoritative matching (against real state) avoids hallucinated names. The round-trip cost (one extra turn) is acceptable given this only happens on ambiguous commands.

**Alternatives considered**:
- Let the primary pick → rejected: violates FR-010 "never silently guess". Even if the model is 90% accurate, we can't guarantee it without authoritative state.
- Require exact task IDs → rejected: user wants natural-language names; violates Principle II.

## R6. Migration idempotency marker

**Decision**: Store `migrated_v0_23_0: <iso8601>` inside each `data/continuous/<name>/state.json` at successful migration. The migration reads this field before acting; presence = skip. Additionally, a process-wide marker file `data/migrations/v0_23_0.done` is written after every per-task iteration completes without unhandled error.

**Rationale**: Per-task marker makes the migration safe to re-run on partial failures (e.g., crash mid-run) — already-migrated tasks are skipped, remaining tasks proceed. The process-wide marker is used by the migration runner (`bot/migrations/runner.py`) to short-circuit the whole module when unambiguously complete.

**Alternatives considered**:
- Only the process-wide marker — rejected: doesn't handle partial mid-run failure.
- Only the per-task marker — acceptable, but we keep the process marker for log clarity and runner consistency.

## R7. Migration platform coverage for sub-topic closure

**Decision**: The migration calls `platform.close_channel(legacy_thread_id)` per task. All three adapters already implement this:
- Telegram: `POST /closeForumTopic` (verified `bot/messaging/telegram.py:246`).
- Discord: `channel.edit(archived=True)` (verified `bot/messaging/discord.py:297`).
- Slack: `conversations_archive` (verified `bot/messaging/slack.py:251`).

On `False` return (operation not supported / already closed / HTTP error), the migration posts a final notice in the legacy sub-topic (`send_to_channel(legacy_thread_id, "🔄 [<name>] migrato nel workspace chat.")`) and logs a WARN. The state file is still updated so future ticks never touch the legacy sub-topic again.

**Rationale**: `close_channel()` is constitutionally part of the ABC (all adapters already expose it). The fallback notice covers platforms/states where close is a no-op but the topic is still visible.

## R8. Parent workspace thread_id resolution

**Decision**: The parent workspace's `thread_id` (plus `chat_id`) is persisted in the workspace registry (`bot/topics.py::load_workspaces()` / `data/workspaces.json`). During migration, for each `data/continuous/<name>/state.json` we look up the owning workspace by the existing `workspace_name`/`parent_workspace_name` field already stored in the continuous state. If the parent cannot be resolved (corrupted state, dangling reference), log an error, skip the task, surface it in a summary post-migration.

**Rationale**: The relationship "continuous task → parent workspace" already exists in state (`parent_workspace` field used by the collab/continuous handlers). No new linkage is needed.

## R9. Secondary-agent plan.md capture

**Decision**: At continuous-task creation (today handled inside `continuous_macro.apply_continuous_macros` → `topics.create_continuous_workspace`), we persist the plan body to `data/continuous/<name>/plan.md`. The plan body is the content inside the `[CONTINUOUS_PROGRAM]…[/CONTINUOUS_PROGRAM]` block produced by the primary agent after user clarification. The secondary agent's prompt template is extended to load and include `plan.md` verbatim in its context in addition to the existing `agents/<name>.md` instructions and the current `state.json`.

**Rationale**: The plan is already captured in the macro payload; we just persist it to a named artifact instead of embedding it only in state. This gives the primary a named file to read when the user asks about the plan (FR-013) and keeps the secondary agent's context single-sourced.

## R10. VERSION bump & migration chain

**Decision**: Bump `VERSION` from `0.22.2` → `0.23.0` (minor bump justified by behavior change + schema repoint). Migration chain adds `bot/migrations/v0_23_0.py` with `from_version="0.22.2"`, `to_version="0.23.0"`. A companion no-op `v0_22_3.py` is NOT required (the chain is version-to-version not release-to-release; jumping 0.22.2 → 0.23.0 is a single step).

**Rationale**: Per constitution V, the chain must be continuous by version tuples, not by every patch release. The existing chain (verified in `bot/migrations/`) skips intermediate patches when no state changed.

## Open questions

None at this stage. All `NEEDS CLARIFICATION` items were resolved during the pre-spec conversation (icons, migration scope, ambiguity behavior).
