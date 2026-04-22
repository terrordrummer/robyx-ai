# Quickstart — Exercise Spec 006 End-to-End

This walkthrough validates the feature on a real deployment. It assumes a Telegram chat connected to Robyx (primary platform target); Discord/Slack follow the documented-degradation path.

## 0. Prerequisites

- Robyx running v0.26.x on the dev machine.
- At least one existing parent workspace (e.g. `zeus-focus-stacking`) with a live Telegram topic.
- `pytest` green on `tests/test_spec_006_quickstart.py`.

## 1. Smoke-test the HQ silence (SC-001, SC-001a)

From HQ (main orchestrator topic):

```
> create a continuous task called "smoke-test-A" in the zeus-focus-stacking workspace
  with objective "run a trivial echo loop for 5 iterations"
  and drain_timeout 120s
```

The orchestrator emits `[CONTINUOUS name="smoke-test-A" objective="…" drain_timeout="120"]`. Expect:

1. **HQ receives**: the orchestrator's natural-language reply confirming creation. That's it.
2. **A new Telegram topic** appears named `[Continuous] smoke-test-A · ▶`.
3. **No scheduler messages** in HQ over the next 10 minutes, regardless of how many step dispatches occur.

Confirm via:

```
> what has happened in the last 10 minutes?
```

Orchestrator emits `[GET_EVENTS since="10m"]`, the handler injects the event list, and the orchestrator renders a summary. The summary MUST include dispatches, step completions, and state transitions.

**Pass criteria**: SC-001 (zero HQ noise) + SC-002 (journal-query coverage).

## 2. Dedicated-topic state markers (SC-003, SC-004, SC-006)

Open the `[Continuous] smoke-test-A` topic. On each step:

- First line of every message matches the header regex in `contracts/delivery-header.md`.
- Topic title suffix changes live: `· ▶` while running, `· ⏸` on awaiting-input, `· ⏳` on rate-limit, `· ✅` on completion.
- Parent workspace topic (`zeus-focus-stacking`) receives **zero** continuous-task messages.

**Pass criteria**: SC-003 (100% dedicated-topic delivery), SC-004 (100% header compliance).

## 3. Awaiting-input pin + reminder (SC-005, SC-006)

Create a task whose first step intentionally ends in awaiting-input (e.g. the agent is asked to propose 3 next-topic options and pause for the user's pick). Observe:

1. Within 10 s of step completion: the delivery message appears in the dedicated topic with `⏸ awaiting input` header, is **pinned** by the bot, and the topic title suffix becomes ` · ⏸`.
2. Simulate 24h silence (or override `AWAITING_REMINDER_SECONDS=60` env var and wait 60 s): exactly one reminder is posted in the same topic referencing the pinned question.
3. Reply in the dedicated topic. On the next scheduler cycle: the pin is removed, the topic title returns to `· ▶`, the task advances.

**Pass criteria**: SC-005 (pin appears ≤ 10 s), SC-006 (exactly one reminder per episode).

## 4. Lifecycle ops (SC-007, SC-008)

Run the following sequence on a task `lifecycle-X` in any workspace:

```
[STOP_TASK name="lifecycle-X"]      # state=stopped, topic preserved, name reserved
[RESUME_TASK name="lifecycle-X"]    # state=running
[STOP_TASK name="lifecycle-X"]      # state=stopped
[CONTINUOUS name="lifecycle-X" ...] # ERROR: name_taken (expected golden message)
[DELETE_TASK name="lifecycle-X"]    # state=deleted, topic renamed to [Archived] lifecycle-X and closed
[CONTINUOUS name="lifecycle-X" ...] # SUCCESS: fresh task, new dedicated topic
[RESUME_TASK name="lifecycle-X-old-deleted"]  # ERROR: not_found (expected golden message)
```

For every ERROR case: the message text matches the "golden messages" in `contracts/lifecycle-ops.md` verbatim (up to local timezone rendering).

**Pass criteria**: SC-007 (name_taken golden message), SC-008 (resume not_found golden message).

## 5. Lock heartbeat & stale recovery (SC-009, SC-010)

With a task `lock-test-A` actively running (in a long step):

1. Identify the subprocess pid: `ps | grep lock-test-A` or read `data/continuous/lock-test-A/lock` line 1.
2. `kill -9 <pid>` (simulate SIGKILL).
3. Within 6 minutes: observe the scheduler reclaim the lock (journaled as `lock_recovered` with outcome=`stale_dead_pid`) and dispatch a fresh step. No bot restart performed.

Separately, simulate bot-down recovery:
1. Stop the bot.
2. Manually create a fake lock: `echo -e "99999\n2026-01-01T00:00:00Z" > data/continuous/lock-test-A/lock`.
3. Start the bot. Observe: first scheduler cycle (not startup) cleans the lock (journaled) and dispatches as normal.

**Pass criteria**: SC-009 (recovery ≤ 6 min), SC-010 (continuous-cycle recovery, not startup-only).

## 6. Workspace-close drain (SC-011)

1. Create a task `drain-test-A` with `drain_timeout=180s`.
2. While its step is mid-run (≥ 60 s into execution), close the parent workspace.
3. Observe: the step is allowed to finish; its output is delivered to the dedicated topic with a `⚠ workspace closed` header, or — if the task was concurrently deleted — archived and referenced via a single "drain output recorded in journal" message.

**Pass criteria**: SC-011 (exactly one user-visible message per closure; zero in-flight outputs lost).

## 7. Orphan backoff + incident (SC-012)

Create a task whose step reliably crashes before heartbeating (e.g. `exit 137` on start). Observe three scheduler cycles:

- Cycle 1, 2: `orphan_detected` journal events; no user-visible messages.
- Cycle 3: **one** incident message posted to the task's dedicated topic containing last exit code + last output tail + lock heartbeat info. `orphan_incident` event in journal. Task transitions to `error`. **No further warnings logged** for this task until reset.

**Pass criteria**: SC-012 (exactly one incident message after 3 detections; zero HQ messages; journal contains matching entry).

## 8. Migration idempotency (SC-013)

On a system that was running v0.25.x with ≥ 3 existing continuous tasks sharing parent workspace topics:

1. Upgrade to v0.26.x; observe `v0_26_0.py` runs on first scheduler tick.
2. Each existing continuous task now has a dedicated topic `[Continuous] <name> · <current-state-suffix>`. State files updated in-place.
3. Run the migration manually a second time (`scripts/run_migration.py v0_26_0`): **no-op** on all tasks (idempotency gate). No duplicate topics created.
4. Journal contains one `migration` event per originally-pending task.
5. None of the tasks regressed state; next scheduler cycle dispatches normally.

**Pass criteria**: SC-013 (migration idempotent, no state regressions, no duplicate topics).

## 9. Last-resort HQ surface (SC-001a, FR-002a)

Simulate an unreachable topic by manually deleting a `[Continuous] task-X` topic in Telegram while the task is running:

1. Scheduler detects `TopicUnreachable` on next delivery attempt.
2. Attempts silent recreation via `create_channel`. If **recreation succeeds**: no HQ message; the task continues in the new topic (journal logs the recreation).
3. If recreation fails (simulate by permissions change): `topic_unreachable_since_ts` is set; NO HQ message yet for routine events.
4. When the task's **next event is user-actionable** (e.g. transitions to `awaiting_input` or triggers an `error`): exactly one HQ message appears explaining the situation, and `hq_fallback_sent=true` is recorded to prevent duplicates.
5. Restore topic reachability; next scheduler cycle clears `topic_unreachable_since_ts` and `hq_fallback_sent`. Normal silence resumes.

**Pass criteria**: SC-001a (exactly one HQ message per unreachable episode, only when user-actionable).

## 10. 7-day live run (SC-014, SC-015)

Run the bot for 7 days with ≥ 2 active continuous tasks producing real work:

- At the end of the week, subjectively rate disruption of HQ notifications.
- Query `[GET_EVENTS since="7d"]` and cross-check against `bot.log` dispatch entries: every dispatch/completion/state-transition logged in the journal must correspond to a log line (ordering-preserved).

**Pass criteria**: SC-014 (user rates disruption "not at all"), SC-015 (100% cross-coverage journal ↔ bot.log).

---

## Test-file coverage map (for `/speckit.tasks`)

| Quickstart section | Primary test file |
|---|---|
| §1 HQ silence + `[GET_EVENTS]` | `test_events_macro.py`, `test_hq_fallback.py` |
| §2 Dedicated topic + markers | `test_platform_topic_ops.py`, `test_delivery_header.py` |
| §3 Awaiting-input pin + reminder | `test_awaiting_input_pin.py` |
| §4 Lifecycle ops | `test_continuous_lifecycle.py` |
| §5 Lock heartbeat | `test_lock_heartbeat.py` |
| §6 Workspace close drain | `test_drain_on_close.py` |
| §7 Orphan backoff | `test_orphan_backoff.py` |
| §8 Migration | `test_migration_v0_26_0.py` |
| §9 HQ last-resort | `test_hq_fallback.py` |
| §10 Live-run (manual) | `test_spec_006_quickstart.py` (E2E harness) |
