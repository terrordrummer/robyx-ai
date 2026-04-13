# Robyx Operational Backlog

This file is the local source of truth for the post-review remediation plan.
Work one ticket at a time. Keep scope tight. Do not start dependent work early.

## Working Rules

- Status values: `todo`, `in_progress`, `done`, `blocked`
- Priorities: `P0` critical contract/runtime issues, `P1` important correctness/alignment, `P2` hardening and cleanup
- Definition of done:
  - code implemented
  - tests added or updated
  - documentation aligned with actual behavior
  - no open contradiction left between code and docs for the touched area

## Epics

| Epic | Goal |
|------|------|
| `E1` | Reminder platform parity and runtime safety |
| `E2` | Reliable scheduled execution and delivery into agent topics |
| `E3` | Scheduling lifecycle and validation hardening |
| `E4` | Workspace model and documentation alignment |

## Recommended Execution Order

1. `BL-00` product-contract decision
2. `BL-01` reminder transport abstraction
3. `BL-02` reminder loops on Slack/Discord
4. `BL-03` reminder concurrency fix
5. `BL-04` scheduled-task output delivery into topics
6. `BL-05` reject invalid one-shot scheduling input
7. `BL-06` cancel pending timed work on workspace close
8. `BL-07` consolidate the workspace `work_dir` model
9. `BL-08` align `.env.example` and config docs
10. `BL-09` extend regression coverage
11. `BL-10` final documentation/changelog pass

## Product Decisions

These decisions were made in `BL-00` and are the implementation contract for
the remediation work below.

### D1: Text reminders stay cross-platform

- Decision: keep the product promise and implement it
- Contract:
  - text reminders are a first-class capability on Telegram, Slack, and Discord
  - the reminder engine must send through the platform abstraction, not through Telegram-only bot APIs
  - documentation keeps describing reminders as a universal skill
- Reason:
  - this is already the user-facing promise in the product docs
  - reducing it to Telegram-only would be a regression of the intended platform model

### D2: Scheduled work must deliver into the target topic/channel

- Decision: keep the product promise and implement it
- Contract:
  - periodic tasks, one-shot tasks, and `[REMIND agent="..."]` executions must produce user-visible output in the target workspace/specialist topic
  - log files remain an operational artifact, not the primary delivery path
- Reason:
  - "runs autonomously but only writes to logs" is not a viable workspace-agent experience
  - current documentation and prompt contract already assume visible delivery

### D3: Consolidate on a consistent stored `work_dir`, but do not add a new per-workspace path-selection feature in this remediation

- Decision: implement consistency, reduce overclaiming
- Contract:
  - every agent keeps a stored `work_dir`
  - every execution path, including scheduled execution, must honor the agent's stored `work_dir`
  - workspace creation continues to seed `work_dir` from the global `ROBYX_WORKSPACE`
  - Robyx does not, in this remediation, gain a new feature for selecting a distinct filesystem path for each newly created workspace through chat
- Reason:
  - fixing consistency is necessary now; adding full per-workspace path management is a larger feature
  - the current docs overstate automatic project-directory assignment and must be reduced to the actual supported contract

## Tickets

### BL-00

- Status: `done`
- Priority: `P0`
- Epic: `E4`
- Estimate: `0.5d`
- Depends on: none
- Goal: decide the target product contract for:
  - reminder text delivery on all supported platforms
  - scheduled/one-shot/action-reminder output landing in agent topics
  - true per-workspace `work_dir` support vs. simplified global workspace model
- Acceptance criteria:
  - each of the three areas has an explicit keep/change decision
  - the decision is reflected in this file before implementation starts
  - no later ticket proceeds on assumptions not written here
- Resolution:
  - completed on `2026-04-10`
  - decisions captured in `D1`, `D2`, `D3` above

### BL-01

- Status: `done`
- Priority: `P0`
- Epic: `E1`
- Estimate: `1.5d`
- Depends on: `BL-00`
- Goal: make reminder delivery use the platform abstraction instead of Telegram-only bot APIs
- Files likely touched:
  - [bot/reminders.py](/Users/rpix/Workspace/products/kael-ops/bot/reminders.py)
  - [bot/messaging/base.py](/Users/rpix/Workspace/products/kael-ops/bot/messaging/base.py)
  - platform adapters under [bot/messaging/](/Users/rpix/Workspace/products/kael-ops/bot/messaging)
- Acceptance criteria:
  - reminder engine sends through a platform-neutral interface
  - no Telegram-specific parameter is hardcoded inside the engine
  - existing Telegram behavior remains intact
- Resolution:
  - completed on `2026-04-10`
  - `reminders.py` now sends through `Platform.send_message(...)`
  - text reminders persist `chat_id` in addition to `thread_id`
  - legacy reminder entries without `chat_id` still work via `default_chat_id` fallback

### BL-02

- Status: `done`
- Priority: `P0`
- Epic: `E1`
- Estimate: `0.5d`
- Depends on: `BL-01`
- Goal: run the reminder engine on Slack and Discord, not only on Telegram
- Files likely touched:
  - [bot/bot.py](/Users/rpix/Workspace/products/kael-ops/bot/bot.py)
- Acceptance criteria:
  - Telegram, Slack, and Discord all start reminder processing loops/jobs
  - text reminders fire on all supported platforms
  - platform boot paths stay symmetric enough to maintain
- Resolution:
  - completed on `2026-04-11`
  - Slack and Discord now start the reminder engine alongside their other background loops
  - reminder execution is routed through the same `run_reminder_cycle(...)` helper used by Telegram jobs

### BL-03

- Status: `done`
- Priority: `P0`
- Epic: `E1`
- Estimate: `0.5d`
- Depends on: `BL-01`
- Goal: remove event-loop blocking caused by holding a synchronous lock across `await`
- Files likely touched:
  - [bot/reminders.py](/Users/rpix/Workspace/products/kael-ops/bot/reminders.py)
- Acceptance criteria:
  - no `await` executes while a blocking `threading.Lock` is held
  - concurrent reminder append/send flows are safe
  - failure handling still preserves file integrity
- Resolution:
  - completed on `2026-04-11`
  - due reminders are now claimed under the file lock, delivered outside the lock, then reconciled back into the latest file contents
  - stale `sending` claims automatically fall back to `pending`, so failed or interrupted deliveries can be retried safely

### BL-04

- Status: `done`
- Priority: `P0`
- Epic: `E2`
- Estimate: `2.5d`
- Depends on: `BL-00`
- Goal: make periodic tasks, one-shot tasks, and `[REMIND agent="..."]` deliver visible results into the correct topic/channel
- Files likely touched:
  - [bot/scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/scheduler.py)
  - [bot/timed_scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/timed_scheduler.py)
  - [bot/handlers.py](/Users/rpix/Workspace/products/kael-ops/bot/handlers.py)
  - [bot/ai_invoke.py](/Users/rpix/Workspace/products/kael-ops/bot/ai_invoke.py)
- Acceptance criteria:
  - a scheduled task result is posted into its workspace topic
  - a one-shot result is posted into its target topic
  - an action reminder result lands in the target agent topic by default
  - logs remain additive, but no longer act as the only delivery mechanism
- Resolution:
  - completed on `2026-04-11`
  - periodic and timed tasks now start a completion watcher that reads the spawned `output.log`, parses the backend result, and posts it into the task's target topic/channel
  - `[REMIND agent="..."]` executions inherit the target agent thread as before, and the timed-scheduler delivery bridge now makes that result visible in the same topic by default

### BL-05

- Status: `done`
- Priority: `P0`
- Epic: `E3`
- Estimate: `0.5d`
- Depends on: `BL-00`
- Goal: reject one-shot workspaces/tasks without a valid `scheduled_at`
- Files likely touched:
  - [bot/topics.py](/Users/rpix/Workspace/products/kael-ops/bot/topics.py)
  - [bot/timed_scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/timed_scheduler.py)
- Acceptance criteria:
  - invalid one-shot creation is rejected before writing queue state
  - user-visible error explains what is missing or malformed
  - no empty or dead timed-queue entries are produced
- Resolution:
  - completed on `2026-04-11`
  - one-shot workspace creation now validates `scheduled_at` before any channel, file, or queue side effect
  - timed-queue writes reject missing or malformed one-shot `scheduled_at`, so dead queue entries are not created

### BL-06

- Status: `done`
- Priority: `P1`
- Epic: `E3`
- Estimate: `1d`
- Depends on: `BL-04`
- Goal: ensure closing a workspace also neutralizes its pending timed work
- Files likely touched:
  - [bot/topics.py](/Users/rpix/Workspace/products/kael-ops/bot/topics.py)
  - [bot/timed_scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/timed_scheduler.py)
- Acceptance criteria:
  - closing a workspace disables periodic runs
  - queued one-shot/action-reminder work targeting that workspace is canceled or invalidated
  - no closed workspace executes again accidentally
- Resolution:
  - completed on `2026-04-11`
  - closing a workspace now marks pending timed-queue entries for `agents/<workspace>.md` as `canceled`, covering workspace one-shots, reminder-triggered runs, and any pending timed periodic entries targeting that workspace
  - the existing `tasks.md` disable path remains in place for periodic scheduler rows, so both periodic and timed execution paths are neutralized together

### BL-07

- Status: `done`
- Priority: `P1`
- Epic: `E4`
- Estimate: `2d`
- Depends on: `BL-00`, `BL-04`
- Goal: consolidate the `work_dir` model so runtime and docs describe the same supported behavior
- Files likely touched:
  - [bot/handlers.py](/Users/rpix/Workspace/products/kael-ops/bot/handlers.py)
  - [bot/topics.py](/Users/rpix/Workspace/products/kael-ops/bot/topics.py)
  - [bot/agents.py](/Users/rpix/Workspace/products/kael-ops/bot/agents.py)
  - [bot/ai_invoke.py](/Users/rpix/Workspace/products/kael-ops/bot/ai_invoke.py)
  - [bot/scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/scheduler.py)
  - [bot/timed_scheduler.py](/Users/rpix/Workspace/products/kael-ops/bot/timed_scheduler.py)
- Acceptance criteria:
  - interactive and scheduled execution both honor the stored `agent.work_dir`
  - workspace creation behavior is documented honestly: new workspaces inherit from `ROBYX_WORKSPACE`
  - README and orchestrator docs stop implying automatic per-workspace project-directory assignment
  - memory resolution remains coherent with the chosen design
- Resolution:
  - completed on `2026-04-11`
  - scheduled and timed execution now resolve the target agent from stored state, run in that agent's stored `work_dir`, and build memory context from the real workspace/specialist identity instead of the scheduler task slug
  - updating an existing agent now refreshes its stored `work_dir`, so the persisted runtime context stays authoritative
  - README and orchestrator guidance now describe `ROBYX_WORKSPACE` as the default inherited `work_dir`, not an automatic per-workspace project-directory selector

### BL-08

- Status: `done`
- Priority: `P1`
- Epic: `E4`
- Estimate: `0.5d`
- Depends on: `BL-00`, `BL-07`
- Goal: align user-facing config docs and examples with the actual supported configuration contract
- Files likely touched:
  - [.env.example](/Users/rpix/Workspace/products/kael-ops/.env.example)
  - [README.md](/Users/rpix/Workspace/products/kael-ops/README.md)
  - [ORCHESTRATOR.md](/Users/rpix/Workspace/products/kael-ops/ORCHESTRATOR.md)
- Acceptance criteria:
  - `.env.example` includes the expected cross-platform keys
  - README and orchestrator docs no longer overpromise unsupported behavior
  - config examples are internally consistent
- Resolution:
  - completed on `2026-04-11`
  - `.env.example` now documents `ROBYX_PLATFORM`, timed/update intervals, and the Telegram compatibility placeholders that Slack/Discord installs still need at startup
  - README, setup help, and the orchestrator brief now describe the real platform credential contract instead of implying Discord control-room auto-creation or one-token platform migrations in manual/non-interactive flows

### BL-09

- Status: `done`
- Priority: `P2`
- Epic: `E1`, `E2`, `E3`, `E4`
- Estimate: `1.5d`
- Depends on: `BL-01`, `BL-02`, `BL-03`, `BL-04`, `BL-05`, `BL-06`, `BL-07`
- Goal: add regression coverage for every issue found in the review
- Files likely touched:
  - [tests/test_reminders.py](/Users/rpix/Workspace/products/kael-ops/tests/test_reminders.py)
  - [tests/test_bot.py](/Users/rpix/Workspace/products/kael-ops/tests/test_bot.py)
  - [tests/test_scheduler.py](/Users/rpix/Workspace/products/kael-ops/tests/test_scheduler.py)
  - [tests/test_timed_scheduler.py](/Users/rpix/Workspace/products/kael-ops/tests/test_timed_scheduler.py)
  - [tests/test_topics.py](/Users/rpix/Workspace/products/kael-ops/tests/test_topics.py)
- Acceptance criteria:
  - failing tests can be written for each original finding
  - all new tests pass after implementation
  - the original regressions are hard to reintroduce silently
- Resolution:
  - completed on `2026-04-11`
  - reminder coverage now includes the send-exception reconciliation path, so failed deliveries do not strand entries in `sending`
  - scheduled-delivery coverage now locks in the visible fallback message when a scheduled run exits cleanly but produces no parseable output
  - workspace-close coverage now exercises the real timed-queue cancellation path, not just the helper call site
  - interactive invocation coverage now verifies the stored `agent.work_dir` is used for memory resolution and subprocess `cwd`, complementing the existing scheduled and timed-run tests

### BL-10

- Status: `done`
- Priority: `P2`
- Epic: `E4`
- Estimate: `0.5d`
- Depends on: `BL-08`, `BL-09`
- Goal: finish the consolidation pass on docs and release notes
- Files likely touched:
  - [README.md](/Users/rpix/Workspace/products/kael-ops/README.md)
  - [ORCHESTRATOR.md](/Users/rpix/Workspace/products/kael-ops/ORCHESTRATOR.md)
  - [CHANGELOG.md](/Users/rpix/Workspace/products/kael-ops/CHANGELOG.md)
- Acceptance criteria:
  - touched behavior is documented only once and consistently
  - changelog records meaningful shipped changes under `Unreleased`
  - no known doc/code contradiction remains open for the implemented scope
- Resolution:
  - completed on `2026-04-11`
  - README, ORCHESTRATOR, and the embedded system prompts now describe the two scheduler loops consistently: periodic work comes from `data/tasks.md`, one-shot work comes from `data/timed_queue.json`, and scheduled runs relay visible output back into the target topic/channel
  - the remaining cross-platform sections now use platform-neutral topic/channel wording instead of describing Robyx as Telegram-only outside the Telegram-specific setup sections
  - `CHANGELOG.md` records the final documentation consolidation under `Unreleased`

## Current Recommendation

Start with `BL-00`, then execute the `P0` tickets in order. Do not touch the
`work_dir` model before the scheduled-task delivery design is clear, because
those changes are tightly coupled.
