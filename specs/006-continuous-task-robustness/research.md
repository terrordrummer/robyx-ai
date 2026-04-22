# Phase 0 Research — Continuous-Task Observability & Lifecycle Robustness

Resolves all technical unknowns introduced by the spec and its clarifications. Every decision below feeds directly into the Phase 1 artifacts.

---

## R1 — Event journal storage format

**Decision**: JSON-Lines (JSONL) file at `data/events.jsonl`, append-only, with hourly+size-based rotation to `data/events/events-YYYYMMDD-HH.jsonl`. Query by tail-scan + parse, bounded to the requested time window.

**Rationale**:
- Fits Robyx constraint *"no external database dependency"* (Principle III).
- Append is atomic per-line on POSIX for line sizes ≤ `PIPE_BUF` (512 bytes on Linux, 4096 on macOS). Every event fits comfortably under 512 bytes because we store structured metadata + short payload, not full step outputs.
- Query pattern is "last N hours, optionally filtered by task/type" — JSONL tail-scan is O(events in window), handles 10³–10⁴ entries per day with no indexing needed.
- Rotation keeps the hot file small (typically ≤ 1 MB) and preserves 7-day history as a handful of shard files.
- Schema evolution is additive: new fields tolerated by older readers (ignored), missing fields take defaults.

**Alternatives considered**:
- **SQLite**: richer queries and indexing, but Robyx has a "no new DB dependency" bias; the memory SQLite is scoped per-agent and already proven. Would duplicate that pattern. Query patterns don't need joins or full-text search.
- **Per-task event log files** (e.g. `data/events/<task>.jsonl`): simpler per-task reads, but cross-task "what happened in the last N hours" queries become O(tasks × scan). The primary use case is cross-task, so a single journal wins.
- **SQLite memory.db extension**: conflates agent memory (semantic entries) with infrastructure events. Poor separation of concerns.

---

## R2 — Event journal schema (task-type-agnostic)

**Decision**: Each line is a JSON object with:

```json
{
  "ts": "2026-04-22T14:30:15.123456+00:00",
  "task_name": "zeus-research",
  "task_type": "continuous",
  "event_type": "step_complete",
  "outcome": "awaiting_input",
  "payload": { "step": 12, "duration_s": 1243, "question": "multi-ref or calibration?" }
}
```

Required fields: `ts` (ISO-8601 UTC), `task_name`, `task_type`, `event_type`, `outcome`. Optional structured `payload` ≤ 1 KB (truncated with `"truncated": true` if larger).

Event-type taxonomy for MVP (continuous tasks):
- `created`, `dispatched`, `step_start`, `step_complete`, `state_transition`
- `stopped`, `resumed`, `completed`, `deleted`, `archived`
- `error`, `orphan_incident`, `orphan_recovery`
- `rate_limited`, `rate_limit_recovered`
- `lock_heartbeat_stale`, `lock_recovered`
- `drain_started`, `drain_completed`, `drain_timeout`
- `hq_fallback_sent`
- `migration`

Event-type hook points reserved (no-op in MVP) for other task types:
- `periodic_fired`, `periodic_completed`
- `oneshot_fired`, `oneshot_completed`
- `reminder_fired`, `reminder_delivered`, `reminder_failed`

**Rationale**: Fixing the schema now with `task_type` required and a published taxonomy makes extension to non-continuous tasks purely additive (per Q5 clarification). Handlers for periodic/one-shot/reminder can simply call `events.append(task_type=..., event_type=..., ...)` when their time comes.

**Alternatives considered**:
- Free-form schema (no required `task_type`): would require a migration when extending to other task types. Rejected.
- Separate schemas per task type: duplicates logic; cross-type queries awkward. Rejected.

---

## R3 — `[GET_EVENTS]` macro grammar and handler contract

**Decision**: Follow the existing `[NOTIFY_HQ attr="value" …]` attribute-style convention (see `bot/ai_invoke.py` `NOTIFY_HQ_PATTERN` and `_COLLAB_ATTR_PATTERN`). Grammar:

```
[GET_EVENTS since="<duration>" task="<name>" type="<event_type>" limit="<int>"]
```

- `since` — REQUIRED; duration string (`"30m"`, `"2h"`, `"1d"`) or ISO-8601 timestamp.
- `task` — OPTIONAL; filter to a single task name.
- `type` — OPTIONAL; filter to one event type (exact match).
- `limit` — OPTIONAL; cap returned events; default 200, max 1000.

Handler contract (see contracts/events-macro.md):
1. Intercept in `handlers.py` pre-response-send (parallel to `_handle_notify_hq`).
2. Strip the macro token from the outward-facing response.
3. Execute query; return a structured summary (chronological, with compact fields).
4. **Inject the result back into the orchestrator's context as a system-role message for the same turn** — so the agent can continue reasoning about it and render a user-facing narrative.
5. On malformed attributes, inject an error message with the same contract (agent can recover, apologise, retry).

**Rationale**: The user chose this pattern in clarify Q1 explicitly (Option A). It's consistent, zero new Claude-tool infrastructure, and proven by `[GET_PLAN]` in continuous workspaces.

**Alternatives considered**: Claude tool-use native schema (rejected per Q1); user slash-command (rejected per Q1); CLI-only (rejected).

---

## R4 — Lock heartbeat format and stale detection

**Decision**:
- Lock file content becomes two lines:
  ```
  <pid>
  <iso8601_heartbeat_ts>
  ```
- Subprocess writes a heartbeat refresh every **30 seconds** (half the stale threshold) using atomic `temp-file + os.replace`.
- Scheduler considers the lock stale when `now - heartbeat_ts > 300 s` (5 min default, configurable via `LOCK_STALE_THRESHOLD_SECONDS`).
- `check_lock()` cleans stale locks on every scheduler cycle (not only at startup). Recovery: delete lock file, transition state `running → orphan` (which flows into existing orphan-detection backoff).

**Rationale**:
- Existing lock carries pid + one-time timestamp; refreshing is a minimal, additive change.
- 30 s heartbeat + 5 min threshold = 10 heartbeats expected per threshold window. Single missed heartbeat (e.g. GC pause, disk stall) does not trigger a false kill; sustained absence does.
- Scheduler already runs every 60 s; stale detection runs within 6 min of actual death (heartbeat interval + threshold + next cycle) — matches SC-009.
- Monotonic-plus-wallclock: we use ISO-8601 wallclock (human-readable in logs); the conservative stale check tolerates clock skew by treating future-dated heartbeats as "just now" (no skew attack relevant for single-user bot).

**Alternatives considered**:
- OS file-lock (`flock`): automatically released on process death, zero heartbeat needed. Rejected because Robyx's subprocesses spawn the bot's own python — no second process to fight over the lock; we need to detect *any* abrupt death including signal kills, and `flock` is platform-specific.
- Touch mtime: refresh by `os.utime`. Similar to timestamp file but loses pid visibility in the file content. Marginal benefit, not chosen.
- Inotify / fswatch: reactive stale detection. Overkill and adds a runtime dependency.

---

## R5 — Telegram Bot API: topic edit / pin / unpin / close

**Decision**: Use these existing Bot API methods (no new external dependency; already transitively available via HTTP):

| Adapter method | Bot API method | Notes |
|---|---|---|
| `create_channel(name)` | `createForumTopic` | Already implemented |
| `close_channel(channel_id)` | `closeForumTopic` | Already implemented (used for archive-on-delete; reopens possible with `reopenForumTopic`) |
| `edit_topic_title(channel_id, new_name)` | `editForumTopic` with `name` only | NEW |
| `pin_message(chat_id, thread_id, message_id)` | `pinChatMessage` with `message_thread_id` | NEW |
| `unpin_message(chat_id, thread_id, message_id=None)` | `unpinChatMessage` (or `unpinAllForumTopicMessages` for bulk) | NEW |
| `archive_topic(channel_id, display_name)` | `editForumTopic(name="[Archived] <display_name>")` + `closeForumTopic` | NEW; composed |

All calls are idempotent at the protocol level (repeating `closeForumTopic` on a closed topic returns a harmless error we log as DEBUG).

**Rate limits**: 30 admin-ish operations/sec per bot on global; within a chat the cap is looser. Topic ops are admin operations; we stay well under the cap since state transitions are rare.

**Failure modes**: If a topic was manually deleted by the user, API returns `Bad Request: TOPIC_NOT_FOUND` (similar). Adapter must map this to a `TopicUnreachable` exception; scheduler catches it and engages the FR-002a last-resort surface path.

**Alternatives considered**: Using a third-party Telegram library beyond current `httpx`-based adapter. Rejected — current adapter is lightweight and works; no reason to add `python-telegram-bot` or `aiogram` for three methods.

---

## R6 — Discord and Slack adapter parity

**Decision**:

**Discord** (`bot/messaging/discord.py`):
- `edit_topic_title` → `channel.edit(name=new_name)` (requires Manage Channels permission).
- `pin_message` → `message.pin()` (works on any text channel; no per-thread filter needed).
- `unpin_message` → `message.unpin()` or `channel.unpin(message_id)`.
- `close_topic` → `channel.edit(archived=True)` for threads; for regular channels, we rename-only.
- `archive_topic` → rename + `archived=True` for threads.

Discord supports all primitives at adapter-compatible fidelity. Full parity achievable.

**Slack** (`bot/messaging/slack.py`):
- `edit_topic_title` → `conversations.rename` (available; requires `channels:manage` scope). Could be OK.
- `pin_message` → `pins.add` (works, but pins are workspace-wide not thread-scoped; UX differs from Telegram).
- `unpin_message` → `pins.remove`.
- `close_topic` / `archive_topic` → `conversations.archive` (permanent-ish; channel unarchive requires admin).

Slack can do most ops but the UX differs: pins are not per-topic-sidebar, and archived channels disappear from most UIs. Decision: implement the methods for Slack too, with a WARN log on first call per session explaining the UX differences (per spec Non-goals).

Per Constitution Principle I (Multi-Platform Parity): we ship functional parity on all three adapters; UX-level parity is documented-limitation on Slack where platform primitives diverge.

**Alternatives considered**: No-op stubs on Discord/Slack with WARN log. Rejected — both platforms have the needed primitives; the cost of writing them is low and avoids a second round of work later.

---

## R7 — Journal rotation and retention

**Decision**:
- Hourly rotation triggered by the scheduler once per cycle if the current hour differs from the filename's hour.
- Size-based safety rotation: if `events.jsonl` exceeds 10 MB, rotate regardless of hour.
- Retention: keep rotated shards ≤ 7 days old; shards older than 7 days are deleted during the same rotation pass.
- Query window queries that straddle rotation boundaries simply read multiple shards.

**Rationale**: 7-day retention matches spec FR-004 and SC-015 live-run window. Hourly rotation keeps the hot file small and bounds query worst-case to ~one shard. 10 MB safety rotation is ~30 000 events at 300 bytes each — well beyond any realistic per-hour volume even with bursts.

**Alternatives considered**:
- Daily rotation: hot file can grow to 10s of MB on active days, slows tail-scans. Rejected.
- Compaction (dedup or aggregate): unnecessary complexity for the retention window and query patterns.
- Archival to cold storage: out of scope; local retention is sufficient.

---

## R8 — Migration strategy (`v0_26_0.py`)

**Decision**: One migration file, idempotent, with four discrete steps:

1. **Initialise journal**: create empty `data/events.jsonl` if missing. Create `data/events/` dir.
2. **For each `data/continuous/*/state.json`**:
   - Load state. If `migrated_v0_26_0` timestamp present, skip.
   - Resolve parent workspace's platform thread_id (already in state or derivable).
   - Call `create_channel("[Continuous] <display_name>")` via Telegram adapter — returns new thread_id.
   - Update state: `dedicated_thread_id = <new>`, `drain_timeout_seconds = 3600` (default), `migrated_v0_26_0 = <now>`.
   - Update the queue entry's `thread_id` to the new dedicated topic.
   - Append a `migration` event to the journal with the old and new thread_ids for provenance.
   - Update topic title with current state marker (` · ▶` / ` · ⏸` / ` · ⏳` / etc.).
   - If task is in `awaiting-input` state with an `awaiting_question`: post a retroactive pinned message into the new dedicated topic with the question, pin it, record `awaiting_pinned_msg_id`.
3. **Slack/Discord adapters**: no-op at the structural level (no topic creation), but rotate delivery routing based on platform capabilities.
4. **Commit state writes atomically** (temp-file + os.replace) after each task migration so a crash mid-migration leaves consistent per-task state and the next run completes the rest.

**Rationale**: Preserves history (the old workspace topic still contains pre-migration messages); adds the new dedicated topic going forward. Idempotent because of the timestamp gate. Partial-failure-safe because state writes are atomic per-task.

**Alternatives considered**:
- Migrate only on next step dispatch (lazy): simpler code but each task's first post-migration event is delayed until its next tick; users would be confused during the gap. Rejected.
- Bulk API calls: Telegram has no bulk topic-create; migration is one-task-one-API-call. Acceptable at realistic scales (≤20 tasks).

---

## R9 — Lifecycle state machine and naming

**Decision**: Introduce an explicit state enum that unifies previously overloaded `"paused"` / `"stopped"` terms. States:

```
created    → running (auto on first dispatch)
running    → awaiting_input (step returned with question)
running    → rate_limited  (API rate-limit hit)
running    → completed      (explicit complete op, or terminal success)
running    → error          (orphan_incident after backoff)
running    → stopped        (explicit stop op)
awaiting_input → running    (user reply + resume)
awaiting_input → stopped    (explicit stop op)
rate_limited   → running    (recovery timestamp passed)
stopped        → running    (explicit resume op)
any state      → deleted    (explicit delete op) → archived_at timestamp set; topic archived; row kept as tombstone
```

Canonical term is `stopped` (not `paused`). Complete is terminal and distinct from stopped (no resume). Edge-case wording in spec updated by clarify already.

**Rationale**: Avoids the ambiguity the user hit ("stop" vs "complete" vs "pause"); gives tasks a single authoritative state field. Handlers validate transitions; invalid transition → user-visible error.

**Alternatives considered**: Keep dual `status` + `sub_status` fields (rejected — complicates queries and spec text).

---

## R10 — Structured delivery header format

**Decision**: Single-line header preceding the body:

```
🔄 [<task_name>] · Step <N>/<M?> · <STATE_EMOJI> <STATE_LABEL> · <HH:MM>
```

- `M` omitted if the task has no predetermined step count.
- State emoji/label pairs:
  - `▶ running`
  - `⏸ awaiting input`
  - `⏳ rate-limited until HH:MM`
  - `✅ completed`
  - `❌ error`
  - `⚠ workspace closed`

Optionally followed by a second line `→ Next: <short description>` when `next_step.description` is set.

Header is computed inside `scheduled_delivery._render_result_message` (single chokepoint per spec 005 convention). The body follows two blank lines later. Total overhead ≤ 120 chars.

**Rationale**: The user explicitly asked for visual distinction and state visibility. A consistent one-line header is parseable by regex (for tests), readable at a glance, and doesn't bloat the message.

**Alternatives considered**:
- Multi-line ASCII box header: visually heavier, more overhead in small deliveries. Rejected.
- Telegram inline-keyboard buttons for state: requires callback_data plumbing that doesn't exist for the bot's current message flow. Out of scope.

---

## R11 — 24-hour awaiting-input reminder

**Decision**: Reminder is opt-in-by-default (fires automatically). Implementation:
- State gains `awaiting_since_ts` (ISO-8601 UTC) set on transition to `awaiting_input` and cleared on resume.
- Scheduler, every cycle, checks tasks in `awaiting_input`: if `now - awaiting_since_ts ≥ 24 h` and `awaiting_reminder_sent_ts` is null, post one reminder (`⏸ Still awaiting your reply on: <question>`), set `awaiting_reminder_sent_ts = now`. Never duplicate.
- If user resumes and the task later returns to `awaiting_input` again (next question), a fresh reminder cycle starts.

**Rationale**: Satisfies FR-011 exactly once per episode. Avoids a separate timer thread — leverages the existing 60 s scheduler cycle.

**Alternatives considered**: Configurable threshold (out of scope for MVP; 24 h is fine for all expected use cases). Escalating reminders (every N hours) — out of scope.

---

## R12 — Chat-first surfaces for new operations

**Decision**: All new operations are invocable via macros from any agent in a workspace OR via lifecycle slash-commands (existing infrastructure):

| Operation | Macro example | User slash |
|---|---|---|
| Create continuous | `[CONTINUOUS name="X" objective="..." drain_timeout="2h"]` | — |
| Stop | `[STOP_TASK name="X"]` | `/stop X` |
| Resume | `[RESUME_TASK name="X"]` | `/resume X` |
| Complete (terminal) | `[COMPLETE_TASK name="X"]` | `/complete X` |
| Delete + archive | `[DELETE_TASK name="X"]` | `/delete X` |
| Query journal | `[GET_EVENTS since="2h"]` | `/events since=2h` (optional future) |

Macros strip as usual; slash commands post confirmation+error messages into the issuing thread. Per Principle II (Chat-First), no file edits required.

**Rationale**: Reuses existing `lifecycle_macros.py` dispatch — minimal new plumbing. Preserves the "never leave the chat" promise.

---

## Summary of decisions → resolved unknowns

| Unknown (from plan Tech Context) | Resolved by |
|---|---|
| Journal storage | R1 |
| Journal schema | R2 |
| Macro grammar | R3 |
| Lock heartbeat format | R4 |
| Telegram API capabilities | R5 |
| Cross-platform parity | R6 |
| Rotation/retention | R7 |
| Migration approach | R8 |
| State machine vocabulary | R9 |
| Delivery header format | R10 |
| Reminder mechanism | R11 |
| Chat-first surface coverage | R12 |

No open `NEEDS CLARIFICATION` remain. Proceed to Phase 1.
