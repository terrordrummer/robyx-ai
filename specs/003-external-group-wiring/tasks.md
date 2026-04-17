---
description: "Task list for feature 003-external-group-wiring"
---

# Tasks: External Group Wiring — Agent ↔ Orchestrator Connection

**Input**: Design documents from `/specs/003-external-group-wiring/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Required by Constitution IV (Comprehensive Testing) — every new public contract MUST have tests. Each user story includes dedicated test tasks; unit-level tests live in `tests/` and may be run as part of the existing pytest suite.

**Organization**: Tasks are grouped by user story (US1 = P1 pre-announced, US2 = P2 ad-hoc setup, US3 = P2 orchestrator↔group routing) so each can land independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: `US1`, `US2`, `US3`; Setup/Foundational/Polish tasks have no story label
- All paths are repository-relative

## Path Conventions

Single-project Python repo. Source under `bot/`; tests under `tests/`; runtime state under `data/`. No `src/` subdirectory. Contract documents live under `specs/003-external-group-wiring/contracts/` and are consumed by the AI backend's system-prompt construction (not shipped as standalone modules).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Light — repo is live, branch `003-external-group-wiring` already exists. No new dependencies, no new directories. Only a short orientation pass.

- [X] T001 Confirm branch `003-external-group-wiring` is checked out and `data/collaborative_workspaces.json` lock file path is writable in the dev environment (manual sanity check; no code change)
- [X] T002 [P] Re-read `bot/handlers.py:1361-1543` (existing `collab_bot_added`) and `bot/collaborative.py:39-385` (existing `CollabWorkspace` / `CollabStore`) before touching either file, to keep the existing race-avoiding ordering in mind

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Pieces that US1, US2, and US3 all depend on. Must be complete before any user-story phase starts.

**⚠️ CRITICAL**: No user story work should start until Phase 2 is green.

### Authorisation & platform abstraction

- [X] T003 Add `is_authorised_adder(user_id: int, collab_store, *, owner_id: int | None) -> bool` helper to `bot/authorization.py`. Returns `True` if `user_id == owner_id` OR if `user_id` has owner/operator role in any existing `CollabWorkspace`. Include a module-level docstring explaining when it's used (external group provisioning).
- [X] T004 Add abstract method `async def leave_chat(self, chat_id: int) -> None` to the `Platform` ABC in `bot/messaging/base.py` with docstring noting that Discord/Slack may raise `NotImplementedError` for this iteration.
- [X] T005 [P] Implement `leave_chat` on the Telegram adapter in `bot/messaging/telegram.py` (call `self._app.bot.leave_chat(chat_id)`).
- [X] T006 [P] Implement `leave_chat` on the Discord adapter in `bot/messaging/discord.py` as `raise NotImplementedError("leave_chat not yet supported on Discord")`.
- [X] T007 [P] Implement `leave_chat` on the Slack adapter in `bot/messaging/slack.py` as `raise NotImplementedError("leave_chat not yet supported on Slack")`.

### Shared `CollabStore` helpers

> The three helpers below extend the same class. Land them as one commit to keep the file's lock/write ordering consistent; they are listed separately to make review easier.

- [X] T008 Add `CollabStore.create_pending(*, name, display_name, agent_name, purpose, parent_workspace, inherit_memory, creator_id) -> CollabWorkspace` to `bot/collaborative.py`. Enforce: name collision → `ValueError`; creator_id == 0 → `ValueError`. Write-path order matches existing pattern: caller writes the seed `data/agents/<name>.md` file BEFORE calling this method; this method only persists the workspace.
- [X] T009 Add `CollabStore.finalize_setup(ws_id, *, parent_workspace, inherit_memory) -> bool` to `bot/collaborative.py`. Guard: current status MUST be `"setup"`. Updates `parent_workspace`, `inherit_memory`, flips `status="active"`, rebuilds `_chat_map`, writes.
- [X] T010 Add `CollabStore.migrate_chat_id(old_chat_id, new_chat_id) -> bool` to `bot/collaborative.py`. Guard: existing record with `old_chat_id` must be in `_ROUTABLE_STATUSES`. Updates `chat_id`, rebuilds `_chat_map`, writes. No status change.
- [X] T011 Add `CollabStore.list_for_orchestrator() -> list[dict]` to `bot/collaborative.py`. Returns list of `{name, display_name, purpose, chat_id, status}` for every workspace NOT in `"closed"`, sorted by `created_at` desc. `purpose` is derived best-effort from the first non-heading line of `data/agents/<name>.md`; falls back to `display_name`.

### Logging & i18n scaffolding

- [X] T012 [P] Add i18n strings to `bot/i18n.py`: `collab_unauthorised_adder`, `collab_unsupported_platform_discord`, `collab_unsupported_platform_slack`, `collab_announce_ok`, `collab_announce_error`, `collab_send_ok`, `collab_send_error`, `collab_setup_complete_hq`, `collab_bot_added_hq_pending`, `collab_bot_removed_hq`, `collab_migrated_hq`, `collab_unauthorised_adder_hq`. Use the existing string-table style (flat dict with `%s`/`%d` placeholders).
- [X] T013 [P] Document the logging prefix convention (`collab.announce`, `collab.match`, `collab.setup.bootstrap`, `collab.setup.complete`, `collab.send`, `collab.notify_hq`, `collab.archive`, `collab.migrate`, `collab.unauthorised`, `collab.unsupported_platform`) as a module-level comment at the top of `bot/collaborative.py` — no behaviour change, just the contract.

### Foundational tests

- [X] T014 [P] Unit tests for `is_authorised_adder` in `tests/test_collab_authorization.py`: owner passes, operator passes, participant rejects, unknown rejects, `owner_id=None` rejects.
- [X] T015 [P] Unit tests for `CollabStore.create_pending` in `tests/test_collaborative.py` (extend existing file): persists, rejects name collision, rejects `creator_id=0`, survives round-trip through `_load()`.
- [X] T016 [P] Unit tests for `CollabStore.finalize_setup` in `tests/test_collaborative.py`: flips `setup→active`, rejects non-setup statuses, persists `parent_workspace` and `inherit_memory`.
- [X] T017 [P] Unit tests for `CollabStore.migrate_chat_id` in `tests/test_collaborative.py`: rebinds chat_id, keeps status, rejects unknown old_chat_id, rejects closed records.
- [X] T018 [P] Unit tests for `CollabStore.list_for_orchestrator` in `tests/test_collaborative.py`: excludes closed, includes setup + active + pending, sorts by created_at desc, falls back to display_name when the agent file has no purpose line.

**Checkpoint**: Phase 2 green → user story phases may begin. US1/US2/US3 can then proceed in parallel as they touch mostly different branches of `bot/handlers.py` and disjoint test files.

---

## Phase 3: User Story 1 — Pre-announced external group gets a working agent (Priority: P1) 🎯 MVP

**Goal**: From HQ, Roberto tells the orchestrator he'll create an external group; the orchestrator captures purpose + inheritance via `[COLLAB_ANNOUNCE]`; when the group is created, Flow A matches and the bot's first in-group message references the pre-announced purpose.

**Independent Test**: End-to-end per `quickstart.md` Flow 1 — announce "nebula" in HQ with purpose and inheritance; create Telegram group; add bot; verify first in-group message references the purpose AND the agent answers follow-ups using inherited skills.

### Tests for User Story 1

- [X] T019 [P] [US1] Parser unit tests for `[COLLAB_ANNOUNCE ...]` in `tests/test_collab_announce_command.py` (NEW): valid input parses to the expected args dict; missing attribute → log+drop; name collision → error trailer emitted; non-HQ / non-owner caller → rejected trailer.
- [X] T020 [P] [US1] Handler integration test in `tests/test_collab_announce_command.py`: feeding an orchestrator response containing `[COLLAB_ANNOUNCE ...]` causes `CollabStore.create_pending` to be called with the right args and `data/agents/<name>.md` to be written with the purpose.
- [X] T021 [P] [US1] Extend `tests/test_collab_handlers.py` Flow-A match test so that after `update_chat_id` the HQ notification string contains the pre-announced purpose (SC-001 assertion).

### Implementation for User Story 1

- [X] T022 [US1] Add `[COLLAB_ANNOUNCE ...]` bracketed-command regex pattern to `bot/handlers.py` (constants section near `DELEGATION_PATTERN` / `FOCUS_PATTERN`). Match attribute-style `key="value"` payload per `contracts/collab-announce.md`.
- [X] T023 [US1] Add `handle_collab_announce(response, chat_id, platform, msg, manager, collab_store) -> str` in `bot/handlers.py` (or co-locate near `_handle_workspace_commands`). On match:
  - Authorise: require HQ main thread AND `msg.user_id == OWNER_ID`; otherwise replace the marker with `[COLLAB_ANNOUNCE rejected: not authorised]`.
  - Write the seed agent file `data/agents/<name>.md` with the `purpose` and inheritance reference.
  - Call `collab_store.create_pending(...)`; on `ValueError` replace marker with `[COLLAB_ANNOUNCE error: <reason>]` and roll back the agent file.
  - On success, replace marker with `[COLLAB_ANNOUNCE ok: name=<name>]`.
  - Log `collab.announce name=<name> creator_id=<id> purpose="..." inherit="..."`.
- [X] T024 [US1] Wire `handle_collab_announce` into the orchestrator response pipeline inside `_process_and_send` (the `is_robyx` branch), invoked before or alongside `handle_delegations` in `bot/handlers.py:438-441`.
- [X] T025 [US1] Extend Flow A at `bot/handlers.py:1384-1436` (the `if pending:` branch of `collab_bot_added`) so the in-group welcome message references `ws.display_name` AND the pre-announced purpose (read back from `data/agents/<ws.agent_name>.md`). Keep the existing welcome shape; only enrich the copy.
- [X] T026 [US1] Extend Flow A's HQ notification in `collab_bot_added` to include the purpose (use new i18n string `collab_bot_added_hq_pending` or extend the existing "Collaborative workspace configured" format with a `Purpose:` line).
- [X] T027 [US1] Update agent-prompt context so the freshly-bound agent has access to the captured purpose. Concretely: verify that the agent's `.md` file written in T023 is the same file `AgentManager` loads when the agent is first invoked (no code change may be needed — document the verification in `tests/test_collab_announce_command.py`).

**Checkpoint**: US1 end-to-end works. Roberto can announce, create, and chat. No Phase 2/other-story regressions.

---

## Phase 4: User Story 2 — Ad-hoc group, AI-driven setup (Priority: P2)

**Goal**: Bot added to a brand-new group without prior HQ announcement. First message is a real AI turn (not a hardcoded template); setup finishes when the agent emits `[COLLAB_SETUP_COMPLETE]`; HQ gets a real notification with captured purpose.

**Independent Test**: Per `quickstart.md` Flow 2 — add bot to fresh group; first message IS an AI turn (two fresh runs produce different wording); reply in group with purpose; agent transitions to active; HQ shows setup-complete notification; survives process restart (SC-006).

### Tests for User Story 2

- [X] T028 [P] [US2] Parser unit tests for `[COLLAB_SETUP_COMPLETE ...]` in `tests/test_collab_setup_complete.py` (NEW): valid input parses; invalid status (`active`/`closed`) logs WARNING and is stripped; missing attribute → log+drop.
- [X] T029 [P] [US2] Handler integration test in `tests/test_collab_setup_complete.py`: a response containing `[COLLAB_SETUP_COMPLETE]` triggers agent file rewrite BEFORE status flip; disk-write failure leaves status as `"setup"` (ordering invariant from FR-008).
- [X] T030 [P] [US2] `tests/test_collab_handlers.py`: extend Flow B test to assert NO hardcoded template string is sent; a mock AI backend invocation is required before any group-facing message. (SC-004 guard.)
- [X] T031 [P] [US2] `tests/test_collab_handlers.py`: after setup-complete is processed, `_handle_collaborative_message` produces an AI-generated reply for the next user message (regression guard on silent-drop failure).

### Implementation for User Story 2

- [X] T032 [US2] Replace the canned-template branch at `bot/handlers.py:1518-1528` (the Flow B `send_message` with hardcoded copy) with a real `_process_and_send(agent, bootstrap_message, chat_id, platform, thread_id=None, is_executive=False)` call. The `bootstrap_message` is the synthesized internal prompt from `research.md` R-02 — store it as a module-level string constant `COLLAB_BOOTSTRAP_PROMPT` near the top of `bot/handlers.py` so it can be referenced in tests.
- [X] T033 [US2] Also in Flow B (`bot/handlers.py:1480-1491`), switch the seed agent file content to a minimal "setup in progress — awaiting `[COLLAB_SETUP_COMPLETE]`" marker so the agent's system prompt is unambiguous during the setup window. Keep the existing rollback-on-OSError semantics at `1492-1503`.
- [X] T034 [US2] Lighten the Flow B HQ notification (currently at `bot/handlers.py:1531-1542`) — it should now say "bot added to group *<title>*, setup in progress" (no faux-orchestration copy). The real notification lands when setup completes (T037).
- [X] T035 [US2] Add `[COLLAB_SETUP_COMPLETE ...]` regex pattern and parser to `bot/handlers.py` (near the other collab patterns from T022).
- [X] T036 [US2] Add `_handle_collab_setup_complete(response, collab_ws, platform, collab_store) -> str` in `bot/handlers.py`. On match, execute the steps from `contracts/collab-setup-complete.md` in order: rewrite `data/agents/<name>.md` with purpose + inherit + inherit_memory; on success call `collab_store.finalize_setup(...)`; on OSError leave status as `setup` and emit a recoverable failure reply in the group; strip the marker from the outgoing response.
- [X] T037 [US2] Wire `_handle_collab_setup_complete` into `_process_and_send` in the **non-robyx collaborative-agent** branch at `bot/handlers.py:443-445` (parallel to `handle_specialist_requests`). Gate the call on `collab_ws is not None` so non-collab specialists are unaffected.
- [X] T038 [US2] After a successful `finalize_setup`, post the real HQ notification using `collab_setup_complete_hq` i18n string (name, display_name, purpose, inherit, inherit_memory, chat_id).
- [X] T039 [US2] Log `collab.setup.bootstrap ws_id=<id>` when Flow B invokes the AI bootstrap, and `collab.setup.complete ws_id=<id> purpose="..."` when the marker is processed. Satisfies FR-012.

**Checkpoint**: US2 end-to-end works. Ad-hoc adds produce real AI turns; setup persists across restart.

---

## Phase 5: User Story 3 — Orchestrator ↔ external group routing (Priority: P2)

**Goal**: Once an external group exists, HQ can list, address, and be addressed by it. Removal / migration keep the registry in sync.

**Independent Test**: Per `quickstart.md` Flow 3 and the latter half of Flow 1 — list external groups from HQ; send a message via `[COLLAB_SEND]`; have the external agent emit `[NOTIFY_HQ]` and see it arrive; remove the bot and verify the group drops off the listing (SC-005).

### Tests for User Story 3

- [X] T040 [P] [US3] Parser unit tests for `[COLLAB_SEND ...]` in `tests/test_collab_orchestrator.py` (NEW): valid input delivers via `platform.send_message`; non-orchestrator caller → rejected trailer; unknown group / non-active group → error trailer; delivery exception → error trailer + WARNING log.
- [X] T041 [P] [US3] Parser unit tests for `[NOTIFY_HQ ...]` in `tests/test_collab_orchestrator.py`: delivers to `CHAT_ID` + `control_room_id`; is stripped by the executive-marker filter when invoked on a non-executive user turn (`is_executive=False`).
- [X] T042 [P] [US3] System-prompt injection test in `tests/test_collab_orchestrator.py`: given 2 active + 1 pending + 1 closed workspace, the orchestrator's rendered prompt context contains an `[AVAILABLE_EXTERNAL_GROUPS]` section with exactly 3 entries (excludes closed), with `purpose`, `chat_id`, and `status` for each.
- [X] T043 [P] [US3] Lifecycle test in `tests/test_collab_lifecycle.py` (NEW): simulated `my_chat_member` "left/kicked" transition triggers `collab_bot_removed`, which closes the workspace and emits HQ notification; group subsequently absent from `list_for_orchestrator`.
- [X] T044 [P] [US3] Lifecycle test in `tests/test_collab_lifecycle.py`: simulated supergroup migration (`migrate_to_chat_id` populated on the update) triggers `collab_bot_migrated`, which rebinds `chat_id` without changing status.
- [X] T045 [P] [US3] Unauthorised-adder integration test in `tests/test_collab_lifecycle.py`: bot added by a non-authorised user → group receives the `collab_unauthorised_adder` message, `platform.leave_chat` is invoked, HQ receives `collab_unauthorised_adder_hq`, no `CollabWorkspace` persisted.

### Implementation for User Story 3

- [X] T046 [US3] Add `[COLLAB_SEND ...]` regex pattern to `bot/handlers.py` (near the other collab patterns).
- [X] T047 [US3] Add `async handle_collab_send(response, chat_id, platform, manager, collab_store) -> str` in `bot/handlers.py`. Semantics per `contracts/collab-send.md`: orchestrator-only gate, `get_by_agent_name` lookup, `active`-status guard, platform delivery, error-trailer mapping, log `collab.send ok=<bool> target=<name>`.
- [X] T048 [US3] Wire `handle_collab_send` into `_process_and_send` in the `is_robyx` branch in `bot/handlers.py:437-441` (alongside `handle_delegations` / `_handle_workspace_commands`).
- [X] T049 [US3] Add `[NOTIFY_HQ ...]` regex pattern to `bot/handlers.py`.
- [X] T050 [US3] Add `async handle_notify_hq(response, collab_ws, platform) -> str` in `bot/handlers.py`. Semantics per `contracts/notify-hq.md`: compose the HQ message with `collab_ws.display_name`, deliver to `CHAT_ID` + `control_room_id`, truncate to 2000 chars, strip marker. Log `collab.notify_hq ws=<name>`.
- [X] T051 [US3] Wire `handle_notify_hq` into the non-robyx collab branch of `_process_and_send`. IMPORTANT: it must run BEFORE `_strip_executive_markers` at `bot/handlers.py:424-425` so executive turns can legitimately emit it, and be stripped on non-executive turns.
- [X] T052 [US3] Inject `[AVAILABLE_EXTERNAL_GROUPS]` section into the orchestrator's system prompt in `bot/ai_invoke.py` (around the orchestrator-context builder at lines 257-303). Render via `collab_store.list_for_orchestrator()` on every turn; format per `research.md` R-04.
- [X] T053 [US3] Add `async collab_bot_removed(platform, chat)` and `async collab_bot_migrated(platform, old_chat_id, new_chat_id)` to `bot/handlers.py` (below existing `collab_bot_added`). Behaviour per `contracts/lifecycle-events.md`: `close(ws_id)` / `migrate_chat_id(...)` then HQ notification then log.
- [X] T054 [US3] Extend the `ChatMemberHandler` wiring in `bot/bot.py:461-476` to dispatch to `collab_bot_added` / `collab_bot_removed` / `collab_bot_migrated` based on the `my_chat_member` status transition. Keep the existing handler exported name for backward compat.
- [X] T055 [US3] Add the unauthorised-adder guard at the top of `collab_bot_added` in `bot/handlers.py`: call `is_authorised_adder(added_by_id, collab_store, owner_id=getattr(_config, "OWNER_ID", None))`; on `False` — send `collab_unauthorised_adder` in the group, `await platform.leave_chat(chat_id)`, notify HQ, log `collab.unauthorised chat=<id> by=<id>`, return. No `CollabWorkspace` is persisted.

**Checkpoint**: US3 end-to-end works. Registry stays in sync with reality; two-way routing works.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: FR-013 multi-platform guard, release notes, constitutional compliance audit, quickstart validation.

- [X] T056 [P] Discord `on_guild_join` handler in `bot/messaging/discord.py`: send `collab_unsupported_platform_discord` in the first text channel found, log `collab.unsupported_platform platform=discord guild=<id>`. Do not auto-leave.
- [X] T057 [P] Slack `member_joined_channel` handler in `bot/messaging/slack.py` (for the bot user): send `collab_unsupported_platform_slack` in the channel, log `collab.unsupported_platform platform=slack channel=<id>`. Do not auto-leave.
- [X] T058 [P] Unit tests for Discord/Slack stubs in `tests/test_collab_multiplatform.py` (NEW): message is sent exactly once; no `CollabWorkspace` is persisted; log entry present.
- [X] T059 Document the Telegram-only scope + follow-up items (Discord/Slack parity, optional "stay+await approval" flow) in `releases/vX.Y.Z.md` draft. Include the Constitution I justified-violation entry.
- [X] T060 Update `CHANGELOG.md` with a Feature entry referencing `003-external-group-wiring` and the user-visible behaviour changes.
- [X] T061 [P] Update `data/agents/robyx.md` (orchestrator system instructions) to document the new `[COLLAB_ANNOUNCE]` and `[COLLAB_SEND]` commands so the orchestrator actually knows to emit them. Cross-link the contracts under `specs/003-external-group-wiring/contracts/`.
- [X] T062 [P] Update the collaborative-agent default instructions template (used by Flow B bootstrap; also used by agents registered via `create_pending`) to document `[COLLAB_SETUP_COMPLETE]` and `[NOTIFY_HQ]`. Live in a new `data/agents/_templates/collab.md` or similar — verify with a test that the bootstrap prompt composition references the right template.
- [X] T063 Run `pytest tests/` to confirm zero regressions; investigate and fix anything that breaks.
- [X] T064 Run `ruff check bot/ tests/` and resolve new lint warnings introduced by this feature.
- [X] T065 Execute `quickstart.md` flows 1–5 against a running bot in a scratch environment; capture observations in the PR description.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** → immediate.
- **Foundational (Phase 2)** → depends on Phase 1; **BLOCKS** US1/US2/US3.
- **User Stories (Phases 3–5)** → depend on Phase 2. Can run in parallel if staffed (different engineers, different handler branches, mostly different test files). If sequential, order by priority: **US1 (P1) → US2 (P2) → US3 (P2)**.
- **Polish (Phase 6)** → depends on all targeted user stories being complete.

### Within Phase 2 (Foundational)

- T003 (is_authorised_adder) independent of T004-T007 (Platform.leave_chat)
- T008-T011 (CollabStore helpers) all touch `bot/collaborative.py` — land sequentially in one commit to avoid merge churn
- T012 (i18n) and T013 (logging doc) independent of everything → [P]
- T014 depends on T003; T015-T018 depend on T008-T011

### Within each user story

- Tests (T019-T021 for US1, T028-T031 for US2, T040-T045 for US3) SHOULD fail first, then implementation makes them pass (Constitution IV discipline).
- Implementation order within a story follows the task numbers (parser regex → handler function → wire into `_process_and_send`/event pipeline → enriched HQ copy → log lines).

### Parallel Opportunities

- Phase 2: T005/T006/T007 (different adapter files), T012/T013/T014 (different files), T015-T018 (different test methods within same file — parallel if running per-test).
- Phase 3 (US1): T019/T020/T021 (all test tasks in one new file, can be written together).
- Phase 4 (US2): T028/T029/T030/T031 (different test classes/methods).
- Phase 5 (US3): T040/T041/T042/T043/T044/T045 (independent test scenarios).
- Phase 6: T056/T057/T058 (different platform files), T061/T062 (different agent instruction files).

### Cross-story file conflicts to watch

- `bot/handlers.py` is touched by US1, US2, and US3 — land them in sequence or coordinate branches. Sections are disjoint (orchestrator command block vs non-robyx collab branch vs event dispatch), so conflicts are resolvable but not trivial.
- `bot/collaborative.py` is only touched in Phase 2.
- `bot/ai_invoke.py` is only touched in US3 (T052).
- `bot/bot.py` is only touched in US3 (T054).

---

## Parallel Example: User Story 1 tests

```bash
# All US1 test scaffolding can be written in parallel:
Task: "Parser unit tests for [COLLAB_ANNOUNCE] in tests/test_collab_announce_command.py"
Task: "Handler integration test (create_pending invocation, agent file write) in tests/test_collab_announce_command.py"
Task: "Flow-A HQ notification assertion in tests/test_collab_handlers.py"
```

## Parallel Example: Phase 2 platform adapters

```bash
# Independent files, no shared dependency:
Task: "Implement leave_chat on Telegram adapter in bot/messaging/telegram.py"
Task: "Implement leave_chat on Discord adapter in bot/messaging/discord.py (NotImplementedError)"
Task: "Implement leave_chat on Slack adapter in bot/messaging/slack.py (NotImplementedError)"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 + Phase 2 → foundation ready.
2. Phase 3 (US1) → Roberto can pre-announce groups. **STOP & validate** against Flow 1 of `quickstart.md`. Merge → ship MVP.
3. Phase 4 (US2) → ad-hoc adds produce real AI turns. Validate Flow 2. Merge.
4. Phase 5 (US3) → full bidirectional routing + lifecycle. Validate Flows 3, 4, 5.
5. Phase 6 → polish, release notes, multi-platform guard.

### Parallel Team Strategy

If multiple engineers are available after Phase 2:

- Dev A → US1 (handlers.py orchestrator branch + Flow A enrichment)
- Dev B → US2 (handlers.py non-robyx branch + Flow B replacement + setup-complete)
- Dev C → US3 (ai_invoke.py injection + collab_send/notify_hq + lifecycle events + bot.py wiring)
- Stories merge independently via the checkpoints at the end of each phase.

---

## Notes

- `[P]` tasks = different files, no dependencies on in-progress work.
- Tests are NOT optional (Constitution IV). Every new regex/handler/CollabStore method has a test.
- Commit per task or per logical group; each checkpoint is a safe cutoff.
- Preserve existing race-avoiding order in `collab_bot_added` and in `finalize_setup`: write agent `.md` BEFORE flipping/persisting status.
- Do NOT introduce a schema change to `CollabWorkspace` — all new information fits existing fields.
- FR-013's Telegram-only scope is a **justified** Constitution Principle I violation — documented in `plan.md` Complexity Tracking and release notes (T059).
