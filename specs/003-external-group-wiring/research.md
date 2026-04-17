# Phase 0 Research: External Group Wiring

**Feature**: 003-external-group-wiring
**Date**: 2026-04-16

This document resolves the technical unknowns identified in `plan.md`'s Technical Context and records the design rationale for each decision. No `NEEDS CLARIFICATION` markers are open.

---

## R-01 — Pre-announcement capture from the orchestrator in HQ

**Decision**: Introduce a new bracketed control command the orchestrator (agent `robyx`) can emit in its response:

```
[COLLAB_ANNOUNCE name="<stable-slug>" display="<human-readable>" purpose="<short purpose>" inherit="<workspace-name or empty>" inherit_memory="true|false"]
```

Parsed in `bot/handlers.py` alongside the existing `[DELEGATE ...]` / `[FOCUS ...]` handlers (see `ai_invoke.py:755` and `handle_workspace_commands`). On parse, the handler calls `collab_store.create_pending(...)` (new method on `CollabStore`) which persists a `CollabWorkspace` with `status="pending"`, `chat_id=0`, `expected_creator_id=<OWNER_ID>`, `parent_workspace=<inherit>`, `inherit_memory=<bool>`, and a *seed instruction* captured from `purpose` stored in `data/agents/<name>.md` ahead of time.

**Rationale**:
- Reuses the existing orchestrator → handler command pattern; no new delivery channel required.
- The `list_pending_for_creator()` lookup at `handlers.py:1380` already exists; we only need the creation side.
- Authoring the agent `.md` file at announcement time means Flow A (match on bot-added) can skip the "write boilerplate" step and just use the already-captured purpose.
- `expected_creator_id` is the anti-hijack guard per the existing note at `handlers.py:1375-1378`; we bind it to the `OWNER_ID` (the only user who can address the orchestrator in HQ today).

**Alternatives considered**:
- *Slash command in HQ* (e.g., `/collab_announce`): rejected — forces the user to remember exact syntax; loses the AI's ability to infer purpose/inheritance from conversational context.
- *Ephemeral sidecar JSON* written by a tool-use callback: rejected — duplicates state that already belongs in `CollabStore`.

---

## R-02 — Real AI-driven setup turn for Flow B (ad-hoc adds)

**Decision**: Replace the hardcoded template at `handlers.py:1520-1525` with an `invoke_ai(...)` call on the freshly-registered agent. The bootstrap message is an internal prompt the handler synthesizes (not shown to the user), e.g.:

> "You have just been added to a new Telegram group titled *{chat_title}* by user {added_by_id}. No prior announcement exists. Your job now is to (1) greet the group, (2) ask what the workspace should focus on and whether it should inherit from an existing workspace (list available workspaces from `[AVAILABLE_WORKSPACES ...]` context), and (3) emit `[COLLAB_SETUP_COMPLETE ...]` when you have captured enough to proceed. Remain in setup mode until you emit that marker."

The call uses the existing `_process_and_send(agent, bootstrap_message, chat_id, platform, thread_id=None, is_executive=False)` wrapper so typing indicators, interrupt handling, and tool-marker stripping all come for free.

**Rationale**:
- Produces a real AI turn — per SC-004, every external-group creation must receive an AI-generated greeting, not a byte-identical template.
- The agent already has `agent_type="workspace"` and can inherit the standard workspace system prompt via the existing AI backend plumbing; no special "setup-only" code path needed in `ai_invoke.py`.
- `is_executive=False` mirrors how participant messages are treated in existing collaborative flows — protects against prompt-injection-driven executive actions during setup.

**Alternatives considered**:
- *Dedicated "setup agent" singleton* that does setup for all groups and then hands off: rejected — adds coordination complexity and loses the "same agent the group will talk to forever" identity.
- *Two-step: template first, AI later when the user replies*: rejected — this is the current behaviour and exactly the failure mode Roberto reported.

---

## R-03 — Setup completion marker and state transition

**Decision**: The setup agent emits:

```
[COLLAB_SETUP_COMPLETE purpose="<captured purpose>" inherit="<workspace or empty>" inherit_memory="true|false"]
```

Handler-side processing (new `_handle_collab_setup_complete` called from `_process_and_send` before sending the response) performs, in order:

1. Rewrite `data/agents/<name>.md` with the captured purpose + inheritance reference.
2. Call `collab_store.finalize_setup(ws_id, parent_workspace=..., inherit_memory=...)` which atomically updates `parent_workspace`, `inherit_memory`, and flips `status="setup" → "active"`.
3. Post a real notification to HQ summarising the group (name, purpose, members-so-far, chat_id).
4. Strip the marker from the response before sending to the group.

**Rationale**:
- Ordering (write agent file → flip status) matches the existing race-closing pattern at `handlers.py:1468-1506` (register agent → write file → publish to store). A message arriving between the file write and the status flip still finds a registered agent with correct instructions.
- HQ notification is a real structured message (not "I've been added… I'm asking there"), so the orchestrator's view of the group now has a confirmed purpose.
- The marker is parsed server-side and stripped; the user never sees the raw command string.

**Alternatives considered**:
- *Heuristic detection of "setup is done"*: rejected — unreliable, and the explicit marker fits the same pattern as `[DELEGATE]` / `[FOCUS]` / `[REMIND]`.

---

## R-04 — Orchestrator visibility of external groups (registry → system prompt)

**Decision**: The orchestrator system prompt (constructed in `bot/ai_invoke.py` around line 257-303) is extended with a dynamically-rendered section:

```
[AVAILABLE_EXTERNAL_GROUPS]
- name: nebula | purpose: "Nebula research collab with Alice & Bob" | chat_id: -100123 | status: active
- name: collab-foo | purpose: "setup in progress" | chat_id: -100987 | status: setup
[/AVAILABLE_EXTERNAL_GROUPS]
```

Rendered from `collab_store.list_for_orchestrator()` (new helper that returns active + setup + pending workspaces with the fields the orchestrator needs). Refreshed every turn, so there is no cache that can drift.

**Rationale**:
- The orchestrator sees the authoritative store on every turn; SC-003 (zero manual reconciliation) is inherently satisfied.
- Reusing the system-prompt injection surface means no new coordinator process is needed.
- The rendered block is bounded (only group name, purpose, chat_id, status) — keeps token budget predictable even if the count grows.

**Alternatives considered**:
- *Separate orchestrator tool* ("list_external_groups()"): rejected for this iteration — adds tool-calling complexity for what is effectively a read. Can be promoted to a tool later if token pressure becomes an issue.
- *Static snapshot at bot start*: rejected — violates SC-003 (registry must match reality live).

---

## R-05 — Orchestrator → external group messaging

**Decision**: Add a new `[COLLAB_SEND name="<group-name>" text="<message>"]` control command, parsed in the same handler block as `[DELEGATE]`. On parse:

1. Resolve `ws = collab_store.get_by_agent_name(name)` (existing helper).
2. If `ws is None` or `ws.status != "active"`: append an error result back to the orchestrator's response (same pattern as `handle_delegations` at `ai_invoke.py:754`).
3. Otherwise deliver via `platform.send_message(chat_id=ws.chat_id, text=..., thread_id=None)` and append a short confirmation result.

**Rationale**:
- Mirrors `[DELEGATE]` mechanics and the confirmation-back pattern at `ai_invoke.py:774-789`.
- `get_by_agent_name` already filters on `status=="active"`, preventing stray deliveries to closed groups.
- Delivery failure surfaces in the HQ response so the orchestrator can retry or inform Roberto.

**Alternatives considered**:
- *Direct agent-to-agent invocation* (running the target agent to produce a reply instead of relaying text): rejected — the orchestrator's intent is to *inform* the group, not to trigger an AI turn there. A user reply in the group will naturally produce an agent turn.

---

## R-06 — External group → HQ surfacing

**Decision**: External-group agents can emit `[NOTIFY_HQ text="<summary>"]`. Handler sends `text` to `CHAT_ID` on the orchestrator's control thread (`platform.control_room_id`) prefixed with the group name, and strips the marker from the group-facing response.

**Rationale**:
- Symmetric with `[COLLAB_SEND]`; completes the two-way channel required by FR-007 and User Story 3.
- Reuses the HQ notification path already used at `handlers.py:1421-1428` and `handlers.py:1533-1540`.
- Does not invoke the orchestrator — it lands as a notification in HQ, and Roberto (or the orchestrator on its next turn) can choose to act.

**Alternatives considered**:
- *Re-invoke orchestrator with the notification as input*: rejected for this iteration — risks feedback loops and surprise token cost. Can be added behind a flag later.

---

## R-07 — Lifecycle: bot removed / group deleted / migrated → archive

**Decision**: Extend the existing `ChatMemberHandler` in `bot/bot.py:461-476` to inspect the new `my_chat_member` status. Branch table:

| new status | action |
|-----------|--------|
| `member` / `administrator` after `left` / `kicked` | existing Flow A/B (`collab_bot_added`) |
| `left` / `kicked` | new `collab_bot_removed(chat_id)` → `collab_store.close(ws_id)` + HQ notification |
| `migrated_to_chat_id` (supergroup upgrade) | new `collab_bot_migrated(old_chat_id, new_chat_id)` → rebind `chat_id` on the existing record (no archive) |

**Rationale**:
- `CollabStore.close()` already exists (`collaborative.py:246-254`) and updates `status="closed"`, which removes the group from `_chat_map` via `_rebuild_chat_map()` and from the orchestrator's system-prompt listing (R-04).
- Supergroup migration is the Telegram-specific edge case that would otherwise orphan the group; rebinding preserves history.
- Notification to HQ on archive matches FR-012 (log enough for diagnosis).

**Alternatives considered**:
- *Periodic reconciliation job*: rejected — reactive event handling is simpler and matches existing patterns.

---

## R-08 — Authorisation for provisioning

**Decision**:
- **Pre-announcement** (`[COLLAB_ANNOUNCE]`): only honoured when the orchestrator is invoked by `OWNER_ID`. The orchestrator already runs only from HQ where owner-only gating is enforced at `handlers.py:1049`, so no extra check needed.
- **Flow B (ad-hoc)**: when the bot is added by `added_by_id`, the handler checks `authorization.is_authorised_adder(added_by_id)` (new helper wrapping the existing OWNER_ID / workspace-member checks). Unauthorised adders trigger: (a) bot sends a "not authorised — leaving" message, (b) bot leaves the group, (c) HQ is notified.

**Rationale**:
- Matches FR-011's "MUST be explicit, not silent provisioning".
- The "leave group" Telegram API is available via python-telegram-bot (`bot.leave_chat(chat_id)`); expose it on the `Platform` ABC as `platform.leave_chat(chat_id)` so Discord/Slack can implement or raise.
- Notifying HQ allows Roberto to reverse the decision if it was a legitimate add but by an unrecognised account.

**Alternatives considered**:
- *Provision + await approval from HQ*: rejected for v1 — adds a pending-approval state machine and a new HQ control command. Can be added later if "leave by default" proves too strict.

---

## R-09 — Multi-platform "not yet supported" (FR-013)

**Decision**: Discord and Slack adapters gain an event handler that, when the bot is added to a guild/channel, sends a single message:

> "External collaborative groups are not yet supported on Discord/Slack. Please use Telegram for now. The bot will take no further action in this group."

And emits a log entry. The bot does **not** auto-leave on Discord/Slack (to avoid platform-specific ban / rate-limit surprises), but it does not register any CollabWorkspace and does not reply to follow-up messages either.

**Rationale**:
- Satisfies FR-013's explicit-failure contract.
- Leaving the decision to the user preserves any manual use of the bot they may already have configured.
- Keeps this feature's Discord/Slack footprint tiny — just an event handler + one string.

**Alternatives considered**:
- *Full Discord/Slack support*: deferred per Complexity Tracking in `plan.md`.
- *Silent no-op*: rejected — would recreate the exact silent-failure symptom Roberto reported.

---

## R-10 — Persistence & restart semantics

**Decision**: All new writes go through existing `CollabStore` atomic methods (`add`, `close`, new `create_pending`, new `finalize_setup`, new `migrate_chat_id`). Each uses `_mutex()` → `_write_unlocked()` (temp-file + `os.replace`). Agent `.md` files are written with `AGENTS_DIR.mkdir(parents=True, exist_ok=True)` then `write_text`; on OSError the handler rolls back the agent registration per the existing pattern at `handlers.py:1492-1503`.

**Rationale**:
- Keeps the invariant that survives crash-in-the-middle: either the new pending workspace + its agent file are both present, or neither is.
- No new persistence layer → SC-006 (survives restart) is satisfied by construction.

**Alternatives considered**:
- *SQLite migration*: out of scope; Constitution's "no external database" still applies and the JSON store with locking is already working for this use case.

---

## R-11 — Observability (FR-012)

**Decision**: Each lifecycle transition logs at INFO with a consistent prefix: `collab.announce`, `collab.match`, `collab.setup.bootstrap`, `collab.setup.complete`, `collab.send`, `collab.notify_hq`, `collab.archive`, `collab.migrate`. Error paths log at WARNING/ERROR with the failure reason and the rollback taken.

**Rationale**:
- Grep-friendly prefixes make end-to-end diagnosis possible without reading source, satisfying FR-012.
- Consistent with the existing logger pattern (`log = logging.getLogger("robyx.collaborative")`).

---

## Open items for `/speckit-clarify`

Roberto may want to reconsider:
- **FR-013 scope** — currently Telegram-only; Discord/Slack ship a "not yet supported" message. Could be widened to include Discord/Slack in the same iteration, which would expand Complexity Tracking significantly.
- **Unauthorised adder behaviour** — currently "leave + notify"; could be "stay-but-silent + notify" or "provision pending HQ approval".

Both are safe defaults; no blocking unknowns remain for Phase 1.
