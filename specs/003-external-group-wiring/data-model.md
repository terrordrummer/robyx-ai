# Phase 1 Data Model: External Group Wiring

**Feature**: 003-external-group-wiring
**Date**: 2026-04-16

No new entities are introduced. The feature extends the existing `CollabWorkspace` lifecycle and adds two new helper methods on `CollabStore`. All persistence continues to flow through `data/collaborative_workspaces.json` (single source of truth, per Constitution III).

---

## Entity: CollabWorkspace (existing — `bot/collaborative.py:39`)

Reused as-is. No new fields. Field usage under the new wiring:

| Field | Type | Usage under new wiring |
|-------|------|-----------------------|
| `id` | `str` | Stable internal identifier (`collab-<uuid8>`). Unchanged. |
| `name` | `str` | Stable slug (used as agent name). For pre-announced groups, supplied by the orchestrator via `[COLLAB_ANNOUNCE name="..."]`. For ad-hoc, synthesised from the group title (as today). |
| `display_name` | `str` | Human-readable group title. For pre-announced, from `display="..."` on the announce command; for ad-hoc, from `chat.title`. |
| `agent_name` | `str` | Always equal to `name`. Unchanged. |
| `chat_id` | `int` | `0` while `status=="pending"`, populated on bind. On supergroup migration, rebound. |
| `interaction_mode` | `str` | `"intelligent"` by default; owner can flip to `"passive"` via `/mode`. Unchanged. |
| `parent_workspace` | `str \| None` | **NEW USAGE**: populated from `[COLLAB_ANNOUNCE inherit="..."]` at pre-announcement, or from `[COLLAB_SETUP_COMPLETE inherit="..."]` at the end of Flow B setup. |
| `inherit_memory` | `bool` | **NEW USAGE**: populated from `inherit_memory="true\|false"` on either announce or setup-complete. Default `true`. |
| `invite_link` | `str \| None` | Unchanged. |
| `status` | `str` | Lifecycle expanded — see transitions below. |
| `created_at` | `float` | Unchanged. |
| `created_by` | `int` | User id who added the bot (ad-hoc) or announced (pre-announced). |
| `expected_creator_id` | `int \| None` | **NEW USAGE**: set by `create_pending()` to the announcing user's id; used by existing anti-hijack check at `update_chat_id`. |
| `roles` | `dict[str, str]` | Unchanged. |

### Status lifecycle (extended)

```
                        ┌─────────────┐
  [COLLAB_ANNOUNCE] ──▶ │  pending    │──▶ (bot added, match)  ──▶ active
                        └─────────────┘
                                │
                                └─(expired/cancelled)──▶ (removed)

  bot added (no pending) ──▶ setup ──[COLLAB_SETUP_COMPLETE]──▶ active

  active or setup ──(bot removed / group deleted)──▶ closed

  active ──(supergroup migration)──▶ active (chat_id rebound; status unchanged)
```

**Invariants**:
- `pending`: `chat_id == 0` AND `expected_creator_id != None`.
- `setup`: `chat_id != 0` AND instructions file contains "setup phase" marker OR agent has not yet emitted `[COLLAB_SETUP_COMPLETE]`.
- `active`: `chat_id != 0` AND `status == "active"`.
- `closed`: retained for audit (per existing `purge_closed()` design); excluded from `_chat_map` and orchestrator listing.
- `_ROUTABLE_STATUSES` stays `("active", "setup")` — both routable, matching existing behaviour at `collaborative.py:128`.

---

## New `CollabStore` methods

All methods acquire `_mutex()` and call `_write_unlocked()` for atomicity.

### `create_pending(self, *, name, display_name, agent_name, purpose, parent_workspace, inherit_memory, creator_id) -> CollabWorkspace`

**Purpose**: Persist a pending intent captured from `[COLLAB_ANNOUNCE ...]` before the group exists.
**Invariants on input**:
- `name` must not collide with an existing `CollabWorkspace.name`; collision → `ValueError`.
- `creator_id` must be non-zero; `0` → `ValueError`.
- `parent_workspace`, if provided, should exist in the `AgentManager`; non-existent → log WARNING but still persist (allows orchestrator to pre-announce against a not-yet-created parent).
**Side effects**: writes the seed agent instructions file at `data/agents/<agent_name>.md` with purpose + inheritance reference; on OSError rolls back (does not persist the workspace).
**Returns**: the created `CollabWorkspace` with `status="pending"`, `chat_id=0`, `expected_creator_id=creator_id`.

### `finalize_setup(self, ws_id, *, parent_workspace, inherit_memory) -> bool`

**Purpose**: Apply the output of `[COLLAB_SETUP_COMPLETE ...]`.
**Invariants**:
- Workspace must currently be in `status="setup"`; other statuses → return `False`, log WARNING.
**Side effects**: updates `parent_workspace`, `inherit_memory`; flips `status="active"`; rebuilds `_chat_map`; writes to disk.
**Returns**: `True` on success, `False` on invariant failure.

### `migrate_chat_id(self, old_chat_id, new_chat_id) -> bool`

**Purpose**: Handle Telegram supergroup migration without losing the record.
**Invariants**: old record must exist and be `active` or `setup`; `new_chat_id != 0`.
**Side effects**: updates `chat_id`, rebuilds `_chat_map`, writes.
**Returns**: `True` on success.

### `list_for_orchestrator(self) -> list[dict]`

**Purpose**: Render the live-group registry for injection into the orchestrator's system prompt (R-04).
**Returns**: list of dicts with `{name, display_name, purpose, chat_id, status}`, sorted by `created_at` desc, excluding `closed`. `purpose` is extracted from the agent `.md` file's first non-heading line (best-effort; falls back to `display_name`).

---

## Validation rules (summarised from FRs)

| FR | Validation |
|----|-----------|
| FR-001 | `create_pending` persists before returning; a process crash immediately after announce leaves a recoverable pending record. |
| FR-002 | `list_pending_for_creator(creator_id)` + `update_chat_id(expected_creator_id=...)` must match; creator-mismatch rejected. Already implemented. |
| FR-003 | Flow B MUST NOT send any message to the group before `invoke_ai()` returns. Unit test asserts no `send_message` call with a literal template string. |
| FR-004 | `_ROUTABLE_STATUSES` includes `setup`; follow-up messages route to the live agent. Test: setup-phase message produces an AI reply. |
| FR-005 | `list_for_orchestrator()` is derived from `_workspaces` every call — no separate cache. |
| FR-006 | `[COLLAB_SEND]` resolves via `get_by_agent_name` and fails loudly if the group is not `active`. |
| FR-007 | `[NOTIFY_HQ]` delivers to `CHAT_ID` with `platform.control_room_id`. |
| FR-008 | `finalize_setup` writes the `.md` file BEFORE flipping status (matches existing ordering at `handlers.py:1468-1506`). |
| FR-009 | `finalize_setup` AND `update_chat_id` path both emit an HQ notification with name + purpose. |
| FR-010 | `collab_bot_removed` calls `collab_store.close(ws_id)`; `migrate_chat_id` handles supergroup edge. |
| FR-011 | Unauthorised adder → bot sends refusal, leaves chat, notifies HQ; no `CollabWorkspace` persisted. |
| FR-012 | Every lifecycle event logs with `collab.<verb>` prefix. |
| FR-013 | Discord/Slack adapters send a single "not yet supported" message and do not register a workspace. |
