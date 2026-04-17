# Implementation Plan: External Group Wiring — Agent ↔ Orchestrator Connection

**Branch**: `003-external-group-wiring` | **Date**: 2026-04-16 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/003-external-group-wiring/spec.md`

## Summary

External collaborative groups (non-HQ Telegram groups where Roberto + collaborators work with a dedicated Robyx agent) are today half-wired: when the bot is added to a new group, Flow B at `bot/handlers.py:1438-1542` creates a provisional agent and sends a **hardcoded template** asking "what is this group for?" — but the template is not a real AI turn, and while follow-up messages *are* routed to the agent via `manager.get(collab_ws.agent_name)`, the captured setup answer never updates the agent's instructions, and the orchestrator in HQ never gains an addressable handle on the group. The orchestrator can also not pre-announce a group: Flow A matches by `list_pending_for_creator()` but nothing in the codebase *creates* pending CollabWorkspace records. The user-visible symptoms — "one reply then silence" and "HQ doesn't see the new agent" — are direct consequences.

Technical approach: (1) add a `[COLLAB_ANNOUNCE ...]` control command the orchestrator can emit in HQ to create a pending CollabWorkspace with purpose + optional inheritance; (2) replace the Flow B canned message with a real `invoke_ai()` turn on a bootstrapped setup agent; (3) add a `[COLLAB_SETUP_COMPLETE ...]` marker the setup agent emits when it has captured purpose/inheritance, which atomically updates the agent file, flips status `setup → active`, and notifies HQ with a real summary; (4) expose the live-group registry to the orchestrator's system prompt and add `[COLLAB_SEND ...]` and `[NOTIFY_HQ ...]` control commands for two-way routing; (5) wire the Telegram `my_chat_member` "left/kicked" event to archive the workspace. All state continues to persist in `data/collaborative_workspaces.json` via the existing `CollabStore`.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: python-telegram-bot (for `ChatMemberHandler`), existing internal modules (`bot/agents.py`, `bot/collaborative.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/messaging/*`)
**Storage**: `data/collaborative_workspaces.json` (existing atomic JSON store with fcntl/msvcrt locking, `_write_unlocked` via temp-file + `os.replace`); agent instructions at `data/agents/<name>.md`. No schema change to CollabWorkspace; reuse existing `parent_workspace`, `inherit_memory`, `status`, `expected_creator_id` fields.
**Testing**: pytest (960+ existing tests); new tests land in `tests/` under `test_collab_*` naming. Mock AI backend via the existing test harness pattern in `tests/test_collab_handlers.py`.
**Target Platform**: bot runs as system service (launchd/systemd). Feature is chat-facing; Telegram-only for this iteration per FR-013.
**Project Type**: Single Python project; layout is `bot/` (source) + `tests/` + `data/` (runtime state) + `.specify/` (spec-kit). No `src/` subdirectory.
**Performance Goals**: No new throughput targets. Bot message handling is per-user-turn; setup bootstrap adds one additional `invoke_ai()` call when the bot is added to a new group (already acceptable latency per existing patterns).
**Constraints**: Must preserve single-source-of-truth in `data/`; must survive unclean restart (Constitution III); agent registration/write-file must remain ordered to avoid the existing race guarded at `handlers.py:1468-1506`.
**Scale/Scope**: Roberto's workspace expects O(10) external groups per machine. No scaling work required.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance | Notes |
|-----------|-----------|-------|
| **I. Multi-Platform Parity** | ⚠️ **Justified violation** | Feature ships Telegram-only. Discord/Slack event plumbing for "bot added to group" does not yet exist. Must surface explicit "not yet supported on <platform>" per FR-013, document as a known limitation in release notes, and track a follow-up task. See Complexity Tracking below. |
| **II. Chat-First Configuration** | ✅ Pass | All wiring is driven through chat: pre-announcement via orchestrator in HQ, setup via the group conversation. No file edits or dashboards required. |
| **III. Resilience & State Persistence** | ✅ Pass | All lifecycle transitions persist via existing atomic `CollabStore._write_unlocked`. SC-006 requires setup answers to survive restart; `_handle_collab_setup_complete` writes the updated agent `.md` file and then flips status, matching the existing ordering rule at `handlers.py:1468-1506`. No new mutable in-memory state introduced. |
| **IV. Comprehensive Testing** | ✅ Pass | Plan adds unit tests (pre-announcement creation, Flow B bootstrap, setup-complete marker parsing, archive on bot-removed) and integration tests (orchestrator sees + addresses external groups). Mock AI backend via existing pattern. Golden-path + 2 error cases per user story. |
| **V. Safe Evolution** | ✅ Pass | No schema change to `CollabWorkspace`; all new info fits existing fields (`parent_workspace`, `inherit_memory`, `status`, instructions file). No migration required. Release notes will flag the Telegram-only scope. |

Gate result: **PASS with one documented violation (Principle I — scope)**. Proceed to Phase 0.

## Project Structure

### Documentation (this feature)

```text
specs/003-external-group-wiring/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (control-command grammars, event contracts)
│   ├── collab-announce.md
│   ├── collab-setup-complete.md
│   ├── collab-send.md
│   ├── notify-hq.md
│   └── lifecycle-events.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output — NOT created by /speckit-plan
```

### Source Code (repository root)

```text
bot/
├── handlers.py              # EDIT — Flow B replacement, setup-complete handler,
│                            #        new [COLLAB_*] command parsers, bot-removed event
├── collaborative.py         # EDIT — add `create_pending()`, `finalize_setup()`,
│                            #        `list_active_for_orchestrator()` helpers
├── ai_invoke.py             # EDIT — expose live external-group list to orchestrator
│                            #        system prompt context
├── bot.py                   # EDIT — register `my_chat_member` "left/kicked" branch
├── messaging/
│   ├── base.py              # no change expected
│   ├── telegram.py          # EDIT — ensure invite-link + removal event callbacks
│   ├── discord.py           # EDIT — emit explicit "not yet supported" when bot
│   │                        #        added to a new guild (FR-013)
│   └── slack.py             # EDIT — same as discord.py
└── authorization.py         # EDIT (if needed) — authorise caller for COLLAB_ANNOUNCE

tests/
├── test_collab_handlers.py          # EDIT — Flow B real-AI bootstrap, setup-complete
├── test_collab_orchestrator.py      # NEW — registry visibility + routing contracts
├── test_collab_lifecycle.py         # NEW — bot-added, setup→active, bot-removed→archived
├── test_collab_announce_command.py  # NEW — [COLLAB_ANNOUNCE] parsing + pending creation
└── test_collab_multiplatform.py     # NEW — Discord/Slack "not yet supported" guard
```

**Structure Decision**: Single-project layout (matches the rest of the repo). Code under `bot/`, tests under `tests/`. Control-command contracts live in `specs/003-external-group-wiring/contracts/` as Markdown grammars — they are consumed by the AI backend (system prompt construction) and by handler parsers, not shipped as standalone modules.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **Telegram-only scope (Principle I — Multi-Platform Parity)** | The entire "bot added to group" event exists today only for Telegram (`bot.py:461-476`). Porting to Discord (`on_guild_join`) and Slack (`member_joined_channel`) requires designing three distinct provisioning semantics (Discord guilds vs Telegram groups vs Slack channels differ materially in identity, permissions, and invite semantics) and writing three sets of end-to-end tests. Attempting this in one iteration triples scope and delays the bug fix Roberto is actively blocked on. | Shipping all three platforms at once was considered and rejected: (a) Discord/Slack adapters don't surface the equivalent `ChatMemberUpdated` payload with `added_by` today, requiring additional adapter work outside this feature's scope; (b) Roberto's reported incident is Telegram-only; (c) the contract defined in FR-013 (explicit "not yet supported on <platform>") prevents the silent-failure mode on other platforms, which is the actual risk Principle I guards against. A follow-up feature will extend to Discord/Slack once the Telegram implementation is stable. |
