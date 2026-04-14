# Changelog

## 0.20.12

### Added
- **`bot/migrations/` package** — introduces a version-chained migration framework alongside the legacy name-keyed registry. Each release from 0.20.12 onward ships a matching `vX_Y_Z.py` module with `from_version` / `to_version` / `upgrade()`. The chain must be continuous — a CI-style contract test (`tests/test_migrations_framework.py::TestChainContract`) fails the build if any intermediate release is missing its migration. Multi-version jumps (e.g. 0.20 → 0.25) now safely run every intermediate step in order instead of skipping straight to the newest.
- **`scripts/new_migration.py`** — scaffolds a new `vX_Y_Z.py` with the correct chain links, auto-inferring the previous version from `releases/`.
- **`tests/test_migrations_framework.py`** — 20+ tests for version comparison, tracker persistence, chain discovery / validation, chain execution (including stop-on-error and multi-version jumps), and the release-vs-migration contract.

### Changed
- **`bot/migrations.py` → `bot/migrations/` package** — the old single-module legacy registry is preserved in `bot/migrations/legacy.py` with zero behavioural change; the package `__init__.py` re-exports every previously public name, so `from migrations import run_pending`, `MIGRATIONS_FILE`, `clear_registry_for_tests`, `_rename_to_command_bridge`, etc. continue to work. The legacy `_save_applied` now merges the chain tracker state into the same JSON file instead of overwriting it.
- **Unified `run_pending`** — now runs the legacy registry first (unchanged behaviour), then the version chain, returning a combined list. Boot summaries in chat show both layers.

## 0.20.11

### Changed
- **`bot/ai_backend.py`** — All three backends now default to fully autonomous / unsafe-bypass execution, since Robyx agents run headless and cannot answer approval prompts. **Claude Code** already used `--permission-mode bypassPermissions`; **Codex** now adds `--approval-policy never --sandbox danger-full-access` (overridable via `CODEX_APPROVAL_POLICY` / `CODEX_SANDBOX`); **OpenCode** now writes a managed `opencode-managed.json` at boot with `"permission": "allow"` and points the CLI at it via `OPENCODE_CONFIG` (overridable via `OPENCODE_PERMISSION`, or by setting `OPENCODE_CONFIG` to your own config file). Spawned scheduled tasks force full autonomy regardless of env config, since a scheduled task cannot be approved interactively.
- **`README.md`** — Added a dedicated "Autonomous-by-default permissions" section documenting the three defaults and the Linux/MDM caveat (enterprise `disableBypassPermissionsMode: disable` setting is enforced by Claude regardless of what Robyx asks for).

## 0.20.10

### Changed
- **`bot/ai_invoke.py`** — Extended `STREAM_RETRYABLE_KEYWORDS` to cover transient errors from **all three** supported backends (Claude Code, Codex, OpenCode), not just Claude. Added Node-typical strings (`socket hang up`, `fetch failed`, `network timeout`) and Go/OS-level strings (`context deadline exceeded`, `unexpected eof`) to the list. The retry path itself was already backend-agnostic; this just widens the match so Codex and OpenCode benefit from the same auto-recovery that 0.20.9 added.
- **`tests/test_ai_invoke.py`** — Added a parametrized test (`test_stream_retryable_works_for_all_backends`) that exercises the retry path against all three backend fixtures.

## 0.20.9

### Fixed
- **`bot/ai_invoke.py`** — Transient stream errors from Claude Code (e.g. `Stream idle timeout - partial response received`, typically caused by a macOS sleep/wake cycle breaking the TCP stream) are now detected and automatically retried with a fresh session instead of leaking to chat as `AI Error`. Detection covers all three delivery paths: stderr, stdout, and the case where the CLI returns the error *as* the result payload of a rc=0 run.
- **`bot/handlers.py`** — `typing_task.cancel()` in `_process_and_send` is now awaited (and `CancelledError` swallowed), eliminating a rare race where the cancellation could surface as an unhandled exception.

### Changed
- **`bot/ai_invoke.py`** — Removed the per-invocation typing keep-alive loop from `_invoke_ai_locked`. The continuous typing loop in `bot/handlers.py` (added in 0.20.6) already covers message receipt → response delivery end-to-end, so a second loop inside the locked region was redundant.

## 0.20.8

### Fixed
- **Typing indicator in headquarters** — Replaced PTB `send_chat_action` with direct httpx API call (matching `send_message` pattern). For the General topic, `message_thread_id` is now omitted instead of passing `0`, which caused silent failures.

## 0.20.7

### Added
- **Early typing in `handle_message`** — `send_typing` now fires immediately after initial checks (help, config updates) but before agent routing, giving instant feedback as soon as the message is received.

## 0.20.6

### Fixed
- **Event loop responsiveness** — Converted all synchronous `subprocess.run()` calls to `asyncio.create_subprocess_exec()` in `updater.py`, `scheduler.py`, `topics.py`, and `process.py`. These calls blocked the asyncio event loop for up to 9 minutes during git operations, preventing any message from being processed.

### Added
- **Continuous typing indicator** — A persistent typing loop now runs from message receipt until response delivery, so the user always sees "typing..." while the bot is working.

## 0.20.5

### Changed
- **`README.md`** — Major documentation overhaul: replaced outdated `interactive/scheduled/one-shot` workspace type table with unified description; rewrote Scheduler section with clear explanations of reminders, one-shot, periodic, and continuous tasks; added Agent Interruption section; fixed ASCII diagram (KAEL → ROBYX); updated all env var names to `ROBYX_` prefix with legacy fallback notes; corrected `SCHEDULER_INTERVAL` default (600 → 60); fixed `CLAUDE_PERMISSION_MODE` description; updated project structure (removed deleted `reminders.py`, added `queue.json` and `continuous/`).
- **`bot/handlers.py`**, **`bot/migrations.py`** — Replaced remaining "Kael" references with "Robyx" / "orchestrator" in code comments and migration descriptions.

## 0.20.4

### Added
- **`bot/handlers.py`** — Bare "help" message in Headquarters is now intercepted and handled as `/help`, so users don't need the `/` prefix.

### Changed
- **`bot/i18n.py`** — Improved `/help` text with clearer command descriptions and usage hints.

## 0.20.3

### Fixed
- **`bot/ai_invoke.py`**, **`bot/handlers.py`**, **`bot/agents.py`** — Interrupted agents no longer show "AI Error: unknown". When a user message interrupts a busy agent, the subprocess termination is now recognized via an `interrupted` flag and handled silently — no error is sent to chat, and the user's new message is processed immediately.

## 0.20.2

### Changed
- **`bot/ai_invoke.py`** — Agent interruption now applies to **all** user messages to any busy agent, not just continuous task topics. Any incoming message takes priority over a running subprocess (SIGTERM + 5s grace + SIGKILL), preventing messages from queuing behind the lock. Removed `_is_continuous_task_topic()` helper.

## 0.20.1

### Changed
- **`bot/ai_backend.py`** — Claude Code now defaults to `--permission-mode bypassPermissions` for all invocations (interactive and spawn). Agents operate autonomously without terminal interaction. Override via `CLAUDE_PERMISSION_MODE` env var.

## 0.20.0

### Breaking
- **Unified scheduler** — `bot/timed_scheduler.py` and `bot/reminders.py` deleted. All scheduling logic merged into `bot/scheduler.py` (60s cycle). Single queue file `data/queue.json` replaces `data/tasks.md`, `data/timed_queue.json`, and `data/reminders.json`. Auto-migration at boot.
- **`TIMED_SCHEDULER_INTERVAL`** env var removed. `SCHEDULER_INTERVAL` default changed from `600` to `60`.

### Added
- **Continuous tasks** (`bot/continuous.py`) — autonomous iterative work. Agent executes one step, commits, plans next step, terminates. Scheduler dispatches next step automatically. Dedicated workspace topic (🔄 prefix), git branch in target project repo, state file tracking.
- **Agent interruption** — `Agent.interrupt()` method (SIGTERM + 5s grace + SIGKILL). Any user message to a busy agent interrupts the running subprocess immediately.
- **`[CREATE_CONTINUOUS]` pattern** — orchestrator/workspace agents can spawn continuous tasks with structured program definition.
- **`templates/CONTINUOUS_SETUP.md`** — interview template for continuous task program definition.
- **`templates/CONTINUOUS_STEP.md`** — step execution template with state management instructions.
- **Git branch setup** in `bot/topics.py` — three scenarios: existing repo (create branch), no repo (git init + branch), no git (proceed without versioning).

## 0.19.0

### Changed
- **Full product rebrand from KaelOps/Kael to Robyx.** All user-facing references — install scripts, README, orchestrator docs, agent docs, backlog, and templates — now use the Robyx name. Service identifiers updated: `com.kaelops.bot` to `com.robyx.bot` (macOS launchd), `kaelops` to `robyx` (Linux systemd), `KaelOps` to `Robyx` (Windows Task Scheduler). GitHub clone URL updated to `terrordrummer/robyx-ai`. Dotfile path changed from `.kaelops/` to `.robyx/`. Historical changelog entries are preserved as-is.

## Unreleased

### Added
- **`BACKLOG.md`** — local operational backlog that turns the deep-review findings into an execution plan with priorities, dependencies, acceptance criteria, and a recommended implementation order for working one ticket at a time.

### Changed
- **`bot/reminders.py`** — the reminder engine now routes delivery through the platform abstraction (`Platform.send_message`) instead of calling Telegram-specific bot APIs directly. Text reminders now persist `chat_id` alongside `thread_id`, and legacy entries without `chat_id` still fire through a compatibility fallback when the caller supplies a default destination.
- **`bot/bot.py`** — Slack and Discord now start the same background reminder engine that Telegram already used, so plain text reminders fire on all supported platforms instead of only on Telegram.
- **`bot/reminders.py`** — due reminders are now claimed and reconciled in two phases, so no `await` runs while the blocking file lock is held, concurrent appends are preserved, and stale `sending` claims recover automatically on the next cycle.
- **`bot/scheduler.py`**, **`bot/timed_scheduler.py`**, **`bot/scheduled_delivery.py`** — scheduled runs and timed one-shot runs now relay their parsed AI result from `output.log` back into the target workspace/specialist topic, so logs remain additive but are no longer the only visible delivery path.
- **`bot/topics.py`**, **`bot/timed_scheduler.py`** — one-shot workspace creation and timed-queue writes now reject missing or malformed `scheduled_at` values before any queue/channel side effect, and the existing workspace-creation handler surfaces the specific validation error to the user instead of leaving dead timed-queue entries behind.
- **`bot/topics.py`**, **`bot/timed_scheduler.py`** — closing a workspace now also marks any pending timed-queue rows targeting `agents/<workspace>.md` as `canceled`, so queued one-shots, reminder-triggered runs, and timed periodic entries cannot fire after the workspace has been closed.
- **`bot/task_runtime.py`**, **`bot/scheduler.py`**, **`bot/timed_scheduler.py`**, **`bot/agents.py`**, **`README.md`**, **`ORCHESTRATOR.md`**, **`bot/config.py`** — scheduled and timed runs now resolve the target agent from stored state, execute in that agent's stored `work_dir`, and build memory context from the real workspace/specialist identity; the docs and orchestrator brief now explicitly say new workspaces inherit `KAELOPS_WORKSPACE` by default instead of auto-discovering distinct project directories.
- **`.env.example`**, **`README.md`**, **`ORCHESTRATOR.md`**, **`bot/config.py`**, **`setup.py`** — the documented config contract now matches the runtime more closely: the example env file includes `KAELOPS_PLATFORM`, timed/update intervals, and cross-platform key sections; Slack/Discord compatibility placeholders are documented; Discord control-room setup is no longer described as optional in manual/non-interactive paths; and platform migration guidance now reflects the full credential set instead of implying a one-token swap.
- **`tests/test_reminders.py`**, **`tests/test_scheduled_delivery.py`**, **`tests/test_topics.py`**, **`tests/test_ai_invoke.py`** — regression coverage now locks in the remaining high-risk paths from the review: reminder send exceptions reconcile back to `pending`, scheduled runs still post a visible fallback message when output parsing is empty, `close_workspace` cancels real timed-queue rows, and interactive invocation uses the stored `agent.work_dir` for memory resolution and subprocess execution.
- **`README.md`**, **`ORCHESTRATOR.md`**, **`bot/config.py`**, **`BACKLOG.md`** — the final documentation pass now describes the shipped scheduler contract consistently: `data/tasks.md` is the periodic scheduler source, `data/timed_queue.json` owns one-shot workspaces and timed actions, scheduled output is relayed back into the target topic/channel, and the remaining cross-platform sections no longer describe KaelOps as Telegram-only.
- **`.env.example`**, **`README.md`**, **`ORCHESTRATOR.md`**, **`bot/config.py`**, **`bot/config_updates.py`** — Claude Code no longer runs under a forced permission-bypass default. The config surface now exposes optional `CLAUDE_PERMISSION_MODE`, documents the safer default, and allows explicit chat-driven updates when an operator wants to opt into a non-default Claude mode.
- **`ORCHESTRATOR.md`**, **`bot/config.py`** — timed-task documentation now points agents at `timed_scheduler.add_task(...)` instead of raw JSON appends, matching the runtime helper that validates task names and `agent_file` refs before writing `data/timed_queue.json`.
- **`bot/ai_backend.py`** — Claude Code no longer hardcodes `--permission-mode bypassPermissions`; the flag is set from `CLAUDE_PERMISSION_MODE` in `.env` (or omitted when blank). All backends now route the user message via stdin instead of argv. New `command_stdin_payload` / `spawn_stdin_payload` hooks on the base class.
- **`bot/i18n.py`** — added `config_updated` UI string for direct `.env` key updates from chat.
- **`bot/ai_invoke.py`** — system prompt size is now logged when it exceeds the budget threshold, preventing silent context overflow from large memory archives.

### Fixed
- **Secret-handling path hardening** — inbound chat messages and AI invocation logs no longer record raw message bodies, explicit `KEY=value` config updates are applied directly to `.env` without routing secrets through the AI backend, and Claude prompt payloads now go over stdin instead of argv.
- **Slack workspace/specialist routing** — Slack now stores and persists real channel IDs instead of per-process Python hashes, sanitizes created channel names to Slack-safe slugs, and routes top-level workspace-channel messages by channel id so non-threaded Slack workspaces actually work across restarts.
- **`bot/timed_scheduler.py`** — timed tasks are now claimed and reconciled under a file lock, mirroring the reminders engine so concurrent queue appends are preserved and due tasks cannot dispatch twice if a cycle overlaps with queue writes.
- **Setup/docs contract** — non-interactive Discord setup now requires `--discord-channel-id`, and the public docs no longer claim native Slack slash-command support or request unused Slack `commands` scope.
- **`bot/bot.py`** — Discord timed-scheduler background runs now receive the real platform object instead of crashing on an out-of-scope `plat` reference, so one-shot workspaces and timed reminder actions execute on Discord again.
- **`bot/task_runtime.py`**, **`bot/scheduler.py`**, **`bot/timed_scheduler.py`** — scheduled execution now rejects task names that escape `data/` and `agent_file` refs outside `agents/<name>.md` / `specialists/<name>.md`, preventing malicious queue rows from reading arbitrary local files or writing lock/log artifacts outside the runtime tree.
- **`bot/timed_scheduler.py`** — timed periodic runs now honor existing lock files before dispatch, so a long-running timed job is left `pending` instead of being spawned again on top of itself.
- **`bot/topics.py`** — workspace and specialist display names that would break the machine-parsed markdown tables (`|`, newlines, carriage returns) are now rejected before any channel/file side effect.

## 0.18.0

### Changed
- **`data/tasks.md`** — `system-monitor` removed from the scheduler. The agent is still available for on-demand invocation; only the periodic every-6h run is gone.
- **`install/install-mac.sh`** — after the setup wizard, the installer now runs `git remote set-url --push origin no_push`, setting the push URL to a sentinel value. `git fetch` / `git pull` (used by the auto-updater) are unaffected; any attempt to `git push` from the install directory fails immediately. Closes the accidental-push path that exists when Claude Code is invoked inside the install directory.

### Removed
- **`mkdir -p data/system-monitor`** from `install/install-mac.sh` — the directory was only created to hold periodic snapshot output, which no longer exists.

## 0.17.0

### Added
- **`[REMIND ...]` gains an `agent="<name>"` attribute → action mode.** The reminder pattern now covers "at T *do* that" in addition to "at T *tell me* that". When `agent=` is present, `bot/handlers.py:_handle_remind_commands` routes the entry into `data/timed_queue.json` as a one-shot task targeting the named workspace or specialist, with `text=` as the prompt; when absent, the entry goes into `data/reminders.json` as a plain text reminder (legacy behaviour, unchanged). The two modes are strictly disjoint — a text reminder never touches the timed queue, an action reminder never touches `reminders.json`. At fire time the existing `timed_scheduler.run_timed_cycle` dispatches action entries like any other one-shot task; no new scheduler code path.
- **Action-mode validation runs before any file I/O.** Unknown agent → rejected inline. Agent exists but is not a workspace or specialist (e.g. `kael` itself) → rejected inline. `text=` missing, `in=`/`at=` both present or both absent, invalid duration, naive `at=` → rejected inline. Rejection notices are appended to the user-visible reply, never silently dropped.
- **`thread_id` defaults to the target agent's own topic** in action mode, not the caller's, so the work's output lands where the agent lives. Explicit `thread="<id>"` still overrides.
- **`source: "remind"` additive metadata tag** on REMIND-originated timed-queue entries so logs and future tooling can tell them apart from manually queued tasks. Ignored by every existing consumer; backward-compat verified by a direct `find_due()` probe on mixed old/new queues.
- **`bot/topics.py:RESERVED_AGENT_NAMES` + `_validate_new_agent_name` guard** called from `create_workspace` and `create_specialist` before any side effect. Rejects `kael`, `orchestrator`, the empty string (what `_sanitize_task_name` returns for inputs made entirely of punctuation), and any name already registered in the `AgentManager`. Raises `ValueError` with a specific reason. `bot/handlers.py` catches `ValueError` distinctly from generic failure and surfaces the reason verbatim to the user ("Workspace *kael* not created: cannot create workspace 'kael': name is reserved") instead of the previous generic "Failed to create workspace *kael*". Rejected creations leave no partial state behind — no orphan topic, no half-written `tasks.md` row, no `agents/<name>.md` file.
- **`bot/config.py:_log_models_fallback_source`** — standalone helper called once at module import that emits one `INFO` line covering every branch of the model-preference fallback decision: `models.yaml` present and parsed, file missing with env JSON override, file missing with no env vars (hardcoded defaults — orchestrator/workspace/specialist tiers spelled out), file present but empty/unparseable, file present but PyYAML not installed. A fresh clone with no `models.yaml` no longer silently bills the hardcoded tier — the log file says exactly what's happening.
- **`tests/test_handlers.py::TestHandleRemindAction`** (6 tests) — routing to timed queue, specialist target path resolution (`specialists/<name>.md`), unknown agent rejection with no side effects, Kael type-guard rejection, explicit thread override, mixed text+action in one response.
- **`tests/test_topics.py::TestReservedAndDuplicateNames`** (6 tests) — parametrized reserved name variants (`kael`, `Kael`, `KAEL`, `orchestrator`), empty sanitised name (`!!!`), duplicate workspace with matching sanitisation, duplicate specialist, workspace ↔ specialist cross-namespace collision. Every test asserts no side effects on rejection (channel not created, no files written).
- **`tests/test_model_preferences.py::TestModelsYamlFallbackLogging`** (4 tests) — `models.yaml` present, file missing with env override, file missing with hardcoded fallback, PyYAML not installed.
- **`tests/test_memory.py::TestAppendArchive::test_quarter_naming_covers_all_months`** — parametrized 12-case test that verifies `((month - 1) // 3) + 1` maps every month to the right `QN` suffix. Before v0.17 only the current quarter was exercised.
- **`tests/test_memory.py::TestAppendArchive::test_archive_header_format`** — locks in the UTC timestamp + reason line contract that the memory instructions tell agents to read.

### Changed
- **`bot/config.py`** — all three interactive system prompts (`KAEL_SYSTEM_PROMPT`, `WORKSPACE_AGENT_SYSTEM_PROMPT`, `FOCUSED_AGENT_SYSTEM_PROMPT`) now document the `agent=` action-mode attribute in their `## Reminders` sections, alongside a clearer description of when to use text mode vs. action mode. The structural identity of the section across the three prompts is preserved so `tests/test_reminders.py::TestSystemPromptsHaveRemindersSection` continues to guard drift. Model-preference fallback logic refactored out of inline module code into `_log_models_fallback_source` for testability.
- **`bot/handlers.py:_handle_remind_commands`** rewritten with a mode switch at the top of the per-match loop: `agent=` present → action path (resolve target, build timed-queue entry, `timed_scheduler.add_task`); absent → text path (build reminder dict, `append_reminder`). The text path is line-for-line identical to v0.16 behaviour apart from thread-default resolution being moved after the mode branch.
- **`bot/handlers.py`** workspace and specialist creation paths now catch `ValueError` distinctly from generic `Exception`. The `ValueError` branch logs as a warning (not an error) and stores the message in `rejection_reason`; the user-facing response interpolates it in place of the generic failure string.
- **`ORCHESTRATOR.md`** `## Reminders` section expanded with: a valid/invalid examples table for the `in=` compact duration grammar (`30s`, `5m`, `2h`, `1h30m`, `2d`, `1d12h`, `1d6h30m15s` vs. `90`, `1.5h`, `30m1h`, `0s`, `100d`), explicit `at=` rules (timezone offset required, naive datetimes rejected, 60 s past tolerance for clock skew), and a new "Reminders with an action" subsection covering the H3 routing, validation, and thread defaults.

### Moved
- **`SCHEDULER.md` → `templates/SCHEDULER_AGENT.md`** via `git mv` (history preserved). The file is the Scheduler *Agent's* system-prompt template, not user documentation about the scheduling system — the previous location and name were misleading. `bot/config.py:SCHEDULER_MD` and the README project tree updated accordingly. Historical references in `CHANGELOG.md` and prior `releases/*.md` are intentionally left untouched as the record of the previous naming.

### Compatibility
- **Non-breaking for users.** Every existing `[REMIND ...]` pattern without `agent=` continues to behave exactly as in v0.16. The action mode is purely additive.
- **Non-breaking for `data/timed_queue.json`.** The new `source: "remind"` field is additive metadata; `find_due` and `dispatch_task` ignore unknown fields. Mixed old/new queues dispatch uniformly.
- **Non-breaking for `[CREATE_WORKSPACE]` / `[CREATE_SPECIALIST]` emitters.** The reserved-name guard only rejects names that would have corrupted `AgentManager` state (overwriting Kael, registering a nameless agent). Any previously-accepted name that wouldn't have broken the bot continues to be accepted.
- **No schema migrations.** `reminders.json`, `timed_queue.json`, `state.json`, `tasks.md`, `specialists.md`, and `agents/<name>.md` are all unchanged in shape.
- **No new dependencies.**
- **Sessions will be reset on first boot post-upgrade** because `bot/config.py` is in `session_lifecycle.GLOBAL_INVALIDATION_FILES` and the system prompts changed. This is the v0.15.1 diff-driven invalidation doing exactly what it was designed to do — zero manual action, the next message to each agent creates a fresh Claude session that bakes in the v0.17 prompt.

## 0.16.0

### Changed
- **Runtime data now lives under `data/`.** Before v0.16, KaelOps shipped `tasks.md`, `specialists.md`, `agents/<name>.md`, and `specialists/<name>.md` at the repo root, committed alongside the source. Any edit the user made to their live fleet diverged from the tracked version, fresh clones inherited personal rows pointing at nonexistent files (the v0.15.2 `zeus-engine` incident), and the user's personal setup was being published to GitHub. v0.16 moves all four targets under `data/` (already gitignored):
  - `tasks.md` → `data/tasks.md`
  - `specialists.md` → `data/specialists.md`
  - `agents/<name>.md` → `data/agents/<name>.md`
  - `specialists/<name>.md` → `data/specialists/<name>.md`
  The in-table agent column still reads `agents/<name>.md`; `scheduler.spawn_task` and `timed_scheduler.dispatch_task` resolve it against `DATA_DIR` rather than `PROJECT_ROOT`, so existing `tasks.md` content is valid verbatim — no content rewrite needed.
- **`bot/config.py` path constants**: `TASKS_FILE`, `SPECIALISTS_FILE`, `AGENTS_DIR`, `SPECIALISTS_DIR`, and `STATE_FILE` are all now derived from `DATA_DIR`. This file is in `session_lifecycle.GLOBAL_INVALIDATION_FILES`, so every agent gets a fresh AI-CLI session on first boot post-upgrade (expected: a bare prompt change to the path constants is still a config change, and the safe default is "reset everything").

### Added
- **`bot/updater.py:migrate_personal_data_to_data_dir()`**, called from `apply_update` between the pre-pull HEAD capture and `git pull --ff-only`. Idempotent: a file that already exists under `data/` is never overwritten. Runs BEFORE the pull so the source files are still in the working tree when they are copied — the pull then removes the now-redundant repo-root copies without data loss. Reports the list of relocated files through `notify_fn` so the user sees the migration in their boot summary.
- **`bot/_bootstrap.py:migrate_personal_data_if_needed()`**, the boot-time safety net. Covers the alternative path where the user runs `git pull && systemctl restart kaelops` manually without going through the auto-updater: tracked files are already gone after the pull, but any **untracked** leftovers (manually created briefs like `agents/zeus-engine.md`) get scooped up on the next boot. Uses only the stdlib because it runs before any third-party imports.
- **`tests/test_updater.py::TestMigratePersonalDataToDataDir`** (9 tests) — covers noop, per-file copy, idempotency (does-not-overwrite), agent/specialist brief collection, ordering guarantee (migration runs before git pull in `apply_update`), and the `notify_fn` integration.
- **`tests/test_bootstrap.py::TestMigratePersonalDataIfNeeded`** (4 tests) — covers noop, repo-root file copy, idempotency, and untracked agent brief rescue.

### Removed
- **`tasks.md`, `specialists.md`, `agents/assistant.md`, `agents/assistant-check.md`, `agents/kael-ops-project.md`, `agents/system-monitor.md`, `specialists/deploy.md`** — the author's personal runtime files that were leaking through the repo since v0.9. The new path for all of them is `data/<same-name>`, and the migration described above handles the move transparently on every existing install. Fresh clones now ship a clean shell: `data/` does not exist in the tree, the bot creates it on first boot, and the fleet starts empty (the user populates it via Kael).

### Compatibility
- **Non-breaking for users.** The auto-updater handles the migration before the pull removes the source files, and the boot-time safety net catches any leftover files on the next restart. No manual action required.
- **Non-breaking for the `tasks.md` format.** Rows keep the existing `agents/<name>.md` column value; the resolver just points at `data/agents/<name>.md` instead of `agents/<name>.md`. Legacy rows migrate with the file.
- **Sessions will be reset** on first boot post-upgrade because `bot/config.py` (a `GLOBAL_INVALIDATION_FILES` entry) changes. This is expected and documented; the personal-data migration is ordered before the pull, so the reset does not lose any fleet state.
- **Working-tree leftovers are not auto-cleaned.** After the upgrade, the old `agents/` and `specialists/` directories may still exist on the runtime install because they contained untracked files. These are now unreferenced; `rm -rf agents specialists` on the runtime install is safe once you have confirmed `data/agents/` and `data/specialists/` contain what you expect. A future release may add an automatic cleanup step.

## 0.15.2

### Fixed
- **The reminder skill from v0.15.0 now actually reaches the existing fleet.** v0.15.0 added the universal `[REMIND ...]` pattern and a one-shot migration to reset every agent's AI-CLI session so the new system prompt would take effect. The migration was tracked as `success` in `data/migrations.json` but agents kept running with the pre-v0.15 system prompt forever — they never emitted `[REMIND]`, the bot's `_handle_remind_commands` was never reached, `data/reminders.json` was never created, and reminders silently failed. Root cause: the migration mutated `data/state.json` directly while the running bot held the pre-mutation copy in memory; the very next `save_state()` call from any interaction wrote the in-memory copy back and clobbered the migration's mutation. v0.15.2 routes every session reset through the new `AgentManager.reset_sessions()` method so the in-memory and on-disk copies stay in sync — `state.json` is **never** mutated outside the AgentManager any more.
- **`bot/bot.py:441` `NameError: name 'manager' is not defined` in `boot_notify`.** A latent bug introduced in v0.14.0 when `heal_detached_workspaces(manager, ...)` was added to the boot path: the `manager` variable lived in `main()` but `boot_notify` is a closure inside `_run_telegram(plat, h, backend)`, so the lookup raised `NameError` on every boot. The exception was caught by the surrounding `try`, the heal step never ran, and the failure was only visible if you grepped `bot.log` for "manager is not defined". The runtime install on this Mac shows it firing at every reboot since v0.14. v0.15.2 threads the `AgentManager` through `_run_telegram` / `_run_discord` / `_run_slack` and captures it in the `boot_notify` closure via the job data, so `heal_detached_workspaces` actually runs again.
- **Removed leaked `zeus-engine` row from `tasks.md`.** Pointed at `agents/zeus-engine.md`, a file that never existed in the repo (it lives in the user's *working* directory, never in the runtime install). Caused two visible failures: (1) on `linknx`, a fresh clone of the repo errored out at every scheduler tick because the agent file did not exist; (2) on the user's Mac runtime install, the same error filled `bot.log` every 10 minutes (`ERROR: Agent file not found`). The other personal entries in `tasks.md` and `specialists.md` are deferred to v0.16, which will untrack those files entirely with a proper backup migration.

### Added
- **`AgentManager.reset_sessions(agent_names: set[str] | None = None) -> list[str]`** in `bot/agents.py`. The only correct way to invalidate agent sessions while the bot is running. Mutates `self.agents` in place using the same convention as the placeholder-UUID sanitiser (`uuid.uuid4()`, `session_started=False`, `message_count=0`), then calls `self.save_state()`. Leaves every other field of every agent verbatim. `None` means "every known agent"; an explicit set names targets and silently ignores unknown names (protecting renames and removals). Returns the sorted list of names actually reset.
- **New migration `0.15.2-reset-sessions-after-clobber-fix`** in `bot/migrations.py`. The v0.15.0 migration is already marked `success` in every existing tracker file, so it will not re-run on the upgrade — but its mutation never actually persisted in production. The new migration ID forces the reset to happen via the (now correct) manager-aware path on the first boot after v0.15.2. Fresh installs that have never seen v0.15.0 will run both migrations in order, with the second as a no-op.
- **`run_pending(platform, manager)`** is the new migration runner signature. Migrations declared via `@migration(...)` now take `(platform, manager)`. Channel-rename migrations accept `manager` and ignore it; state-mutating migrations call `manager.reset_sessions(...)`.
- **`apply_update(version, notify_fn=None, manager=None)`** is the new updater signature. The diff-driven session invalidation introduced in v0.15.1 now routes through `manager.reset_sessions(...)` instead of writing to `state.json` directly. `manager=None` (legacy callers / CLI) skips invalidation with a warning so the update still completes.
- **`bot/session_lifecycle.py:invalidate_sessions_via_manager(manager, changed_paths)`** replaces the v0.15.1 `invalidate_sessions_for_paths(state_file, changed_paths)`. Same decision logic (`agents_to_invalidate` is unchanged) but routes the actual reset through the manager.
- **`tests/test_agents.py::TestResetSessions`** (5 tests) covering the new method itself.
- **`tests/test_agents.py::TestResetSessionsSurvivesSubsequentSaveState`** (3 tests) — the **regression test that should have caught the v0.15.0 bug**. Loads a real `AgentManager` with a stale session_id, runs the reset, simulates a downstream save_state() call, and verifies the fresh session_id survives on disk. Includes `test_direct_state_json_mutation_would_be_clobbered` which **demonstrates** the v0.15.0 bug pattern: it mutates `state.json` directly, calls save_state(), and asserts that the direct mutation is lost.
- **`tests/test_migrations.py::TestResetSessionsAfterClobberFix`** for the new v0.15.2 migration plus a registration-order assertion that 0.15.2 comes after 0.15.0 / 0.14 / 0.12.1.
- **`tests/test_migrations.py::TestRunPending::test_manager_is_passed_to_each_migration`** — guard against future changes to `run_pending` that drop the `manager` arg.
- **`tests/test_session_lifecycle.py::TestInvalidateSessionsViaManager`** with a `_FakeManager` that records every `reset_sessions` call so the tests assert on what the manager *was asked to do*, not on a post-hoc file read.

### Changed
- **`Migration.apply` type signature** changed from `Callable[[Any], Awaitable[bool]]` to `Callable[[Any, Any], Awaitable[bool]]`. All in-tree migrations updated. `clear_registry_for_tests()` is unchanged.
- **`bot/migrations.py:_reset_sessions_for_reminder_skill`** (the v0.15.0 migration) refactored to call `manager.reset_sessions(None)`. External behaviour is the same; the implementation now no longer touches `state.json` directly.
- **`bot/session_lifecycle.py`** trimmed: removed `reset_agent_sessions_in_state` and `invalidate_sessions_for_paths` (the file-I/O entry points). The pure decision function `agents_to_invalidate(changed_paths, known_agent_names)` stays. The new high-level entry point is `invalidate_sessions_via_manager(manager, changed_paths)`.
- **`bot/bot.py`**: `_run_telegram` / `_run_discord` / `_run_slack` now take `manager` as a parameter. `update_check_job` reads the manager from `context.job.data["manager"]` and passes it to `apply_update`. `boot_notify` reads it from `context.job.data["manager"]` and passes it to `run_pending_migrations`. The Telegram boot now passes both `platform` and `manager` to the `run_once(boot_notify, ..., data={...})` call.
- **`bot/handlers.py:cmd_doupdate`** passes `manager=manager` to `apply_update` so manual updates via `/doupdate` get the same in-memory invalidation as auto-updates.
- **`tests/test_updater.py::TestApplyUpdateInvalidatesSessions`** rewritten to assert on a fake `AgentManager` instead of post-hoc reads of `state.json`. The previous version would have continued passing under the broken v0.15.0 / v0.15.1 implementation because it only checked the file on disk — and the file was correct *immediately after* the migration, before any other code touched it.

### Removed
- **`bot/session_lifecycle.py:reset_agent_sessions_in_state`** and **`bot/session_lifecycle.py:invalidate_sessions_for_paths`** — both wrote `state.json` directly and were the source of the v0.15.0 / v0.15.1 silent regression. The pure decision function `agents_to_invalidate` is preserved.
- **`zeus-engine` row from `tasks.md`** (see Fixed).

### Compatibility
- **No breaking changes for users.** The auto-updater applies v0.15.2 like any other release; the new migration regenerates the assistant's session on first boot; the next message creates a fresh Claude session that bakes in the v0.15 system prompt with `[REMIND]`; reminders work end-to-end from there.
- **Developer-facing: the `Migration.apply` signature changed**. Anyone with out-of-tree migrations needs to add a second `manager` parameter (which can be ignored if the migration only touches the platform).
- **Developer-facing: `apply_update` is now keyword-only for the new arg.** Existing callers that did not pass `manager=` continue to work — invalidation is just skipped with a warning.

## 0.15.1

### Added
- **Diff-driven session invalidation in the updater.** `bot/updater.py:apply_update` now captures the pre-pull commit, computes `git diff --name-only <pre>..HEAD` after a successful fast-forward, and passes the changed paths to a new `bot/session_lifecycle.py:invalidate_sessions_for_paths` helper before the service restart. This generalises the one-shot v0.15.0 migration into a permanent updater contract: any future release that touches a system prompt or an agent brief will automatically force the affected agents to start a fresh AI-CLI session — no per-release migration required. Without this, prompt/brief changes were silently swallowed by Claude Code's `--resume` behaviour, exactly the v0.14 → v0.15 regression that v0.15.0 fixed by hand.
- **`bot/session_lifecycle.py`** new module exposing the contract:
  - `GLOBAL_INVALIDATION_FILES` — the frozenset of paths whose change resets every agent (`bot/config.py` for the system prompts, `bot/ai_invoke.py` for the per-agent loader).
  - `agents_to_invalidate(changed_paths, known_agent_names)` — returns `None` for "all known agents" if a global file changed, otherwise a possibly-empty set of named agents whose individual brief was modified.
  - `reset_agent_sessions_in_state(state, agent_names)` — mutates a `state.json`-shaped dict in place, regenerates `session_id`, clears `session_started`/`message_count`, leaves every other field verbatim. `agent_names=None` means "all".
  - `invalidate_sessions_for_paths(state_file, changed_paths)` — high-level entry point used by the updater. Reads, decides, persists, returns the sorted list of agents reset. Never raises on missing/malformed input — the updater must always reach its restart step.
- **Reset granularity is per-agent.** A change to `agents/<name>.md` or `specialists/<name>.md` resets only that one agent. A change to a global file resets the entire fleet. Anything else (Python logic, tests, docs, README) is correctly ignored — those changes are picked up by the process restart that follows `apply_update`, not by a session reset.
- **`notify_fn` reports the reset summary** — when an update resets sessions, the user-facing progress callback receives `Reset AI sessions for N agent(s): name1, name2`, so the boot summary on Telegram makes the side effect visible instead of silent.

### Changed
- **`bot/migrations.py:_reset_sessions_for_reminder_skill`** (the v0.15.0 migration) is refactored to delegate to `session_lifecycle.reset_agent_sessions_in_state(state, None)`. Behaviour is identical — the existing `TestResetSessionsForReminderSkill` tests are the contract — but the implementation no longer duplicates the reset loop. The migration is kept in the registry so installs upgrading directly from `<= 0.15.0` still run it once on first boot; from `0.15.1` onward the updater handles all future cases automatically.
- **`bot/updater.py`** imports `STATE_FILE` from `config` (new) and `invalidate_sessions_for_paths` from `session_lifecycle`. Pre-pull SHA capture failures are logged and skip the invalidation step but never abort the update — the restart still happens.

### Tests
- **New `tests/test_session_lifecycle.py`** with three test classes — 24 tests:
  - `TestAgentsToInvalidate`: global trigger via `bot/config.py`, global trigger via `bot/ai_invoke.py`, per-agent brief, per-specialist brief, unknown agent name ignored, mixed per-agent + per-specialist, global wins over per-agent, unrelated paths ignored, empty diff, subdirectory does not match, non-`.md` files in `agents/` ignored.
  - `TestResetAgentSessionsInState`: global reset with `None`, partial reset with named set, empty target set is no-op, unknown target is no-op, missing `agents` key, `agents` not a dict, skips non-dict agent entries.
  - `TestInvalidateSessionsForPaths`: missing `state.json` is no-op, empty `changed_paths` is no-op, corrupt `state.json` is no-op, global invalidation persists correctly, per-agent invalidation only resets the named agent, irrelevant paths do not rewrite the file, unknown agent brief in diff is no-op, specialist brief invalidation, global wins over per-agent in mixed diff, empty agents dict is no-op.
- **New `TestApplyUpdateInvalidatesSessions` in `tests/test_updater.py`** — 6 end-to-end tests against the real `apply_update` flow with mocked `_git`/`asyncio.create_subprocess_exec`: global trigger resets all, per-agent brief only resets the named agent, irrelevant paths do not touch state, no-state-file does not break update, specialist brief resets only specialist, `notify_fn` reports the reset summary.
- **`_make_git_side_effect` extended** with `pre_pull_sha` and `diff_files` parameters (defaults preserve existing test behaviour), plus new branches for `git rev-parse HEAD` and `git diff --name-only`.
- **`_patch_updater_paths` fixture** now also patches `updater.STATE_FILE` to the per-test `tmp_path / data / state.json`.
- Suite is now **803 passing** (from 768 baseline). The refactored 0.15.0 migration test `TestResetSessionsForReminderSkill` continues to pass unchanged — proof that the helper extraction did not regress the migration's contract.

### Compatibility
- **No breaking changes.** Existing `tasks.md`, `specialists.md`, agent definitions, state files, memory entries, and queue files continue to work unchanged.
- **No new dependencies.**
- **The v0.15.0 migration still runs** on installs upgrading from `<= 0.15.0`, so users that skip 0.15.0 → 0.15.1 directly still get their sessions reset on the first boot after upgrading. From 0.15.1 onward, the updater handles every subsequent prompt/brief change automatically.
- **Update flow now adds one extra `git rev-parse` and one `git diff --name-only`** to `apply_update`. Both are local-only and complete in milliseconds.

## 0.15.0

### Added
- **Universal `[REMIND ...]` skill** for every interactive agent — orchestrator, workspaces, specialists, focused mode. A new declarative pattern parsed centrally by `bot/handlers.py` lets any agent schedule a future text message without ever touching `data/reminders.json` directly. Supports `at="<ISO-8601 with offset>"` and `in="90s|2m|1h30m|2d"` (up to 90 days), an optional `text="..."` body (Unicode allowed), and an optional `thread="<id>"` override that defaults to the topic the agent is currently living in — agents never need to know their own thread id. Multiple `[REMIND ...]` patterns per response are allowed; validation errors surface as inline notices instead of silent drops.
- **`REMIND_PATTERN`, `parse_remind_attrs`, `parse_remind_when`** in `bot/ai_invoke.py`. The parser accepts attributes in any order, normalises `at` to UTC, tolerates 60 s of clock skew on past `at` values, and rejects compound durations longer than 90 days.
- **`_handle_remind_commands(response, agent, thread_id)`** in `bot/handlers.py`, modelled on `_handle_media_commands`: parses every match, builds a reminder dict matching the schema in `bot/reminders.py`, appends via `append_reminder`, strips the pattern from the user-visible reply, and reports parse/append failures inline.
- **`append_reminder(reminders_file, entry)`** helper in `bot/reminders.py` — thread-safe (shares `_lock` with `check_reminders`), atomic (`_save` now writes a `.tmp` file then `replace`s it), creates the file as `[]` if it does not yet exist.
- **"## Reminders" section** identically present in `KAEL_SYSTEM_PROMPT`, `WORKSPACE_AGENT_SYSTEM_PROMPT`, and `FOCUSED_AGENT_SYSTEM_PROMPT` in `bot/config.py`. The existing `## Scheduling Tasks` section in the workspace prompt is reframed at the top to point new users at `[REMIND]` for the common "ping me at T" case and reserves `timed_queue.json` for the rare "re-invoke me at T to do work" use case.
- **New migration `0.15.0-reset-sessions-for-reminder-skill`** in `bot/migrations.py`. v0.14 added per-agent instructions on interactive turns, but Claude Code CLI bakes the system prompt at session creation time and ignores `--append-system-prompt` on `--resume`, so any agent whose session pre-existed v0.14 has never seen the new brief. This migration regenerates `session_id` and clears `session_started` / `message_count` for every agent in `data/state.json`, forcing the next interactive turn to create a fresh AI-CLI session that finally bakes in the v0.15 system prompt with the new "## Reminders" section. Idempotency is delegated to the migrations framework. Fresh installs with no `state.json` are a no-op success.

### Changed
- **`agents/assistant.md` reminder section reduced to a one-line pointer** to the universal skill. The "anticipo intelligente" guidance and `memory.md` workflow stay intact — they are personality, not infrastructure. The personal assistant no longer carries instructions to hand-write JSON to `reminders.json`; it must use `[REMIND ...]` like every other agent.
- **`bot/reminders.py:_save` is now atomic** — writes to a `.tmp` file in the same directory, then `replace`s the target. Concurrent readers in `check_reminders` already share the same `_lock`, so the new `append_reminder` flow inherits race safety for free.
- **`tasks.md` `assistant-check` description** no longer mentions the Python engine, which is now used by the entire fleet rather than only the personal assistant.

### Tests
- New `tests/test_reminders.py` covering `append_reminder` (creates file, appends, atomic temp cleanup, Unicode preservation) plus `TestSystemPromptsHaveRemindersSection` guarding against future drift where one prompt forgets the universal skill.
- New `TestRemindPattern` and `TestParseRemindWhen` in `tests/test_ai_invoke.py`: regex order-independence, Unicode in `text`, multiple-per-response, ISO-with-offset → UTC normalisation, neither/both `at`/`in` rejection, missing-timezone rejection, past-time rejection with 60 s tolerance, malformed datetime, invalid duration, zero duration, over-90-day rejection.
- New `TestHandleRemind` in `tests/test_handlers.py` covering the end-to-end pipeline: pattern strip, file creation, `at`/`in` normalisation, default-thread injection from the live `thread_id`, explicit `thread=` override, multiple-per-response, missing-text and invalid-duration inline-error reporting, no-pattern no-op.
- New `TestResetSessionsForReminderSkill` in `tests/test_migrations.py`: no-state-file no-op, full reset with verification that `thread_id`/`work_dir`/`description`/`created_at` survive verbatim, empty agents dict no-op, corrupt state file → False, registration order check that the 0.15 reset comes after the 0.14 and 0.12.1 renames.

## 0.14.0

### Carried through
- All v0.12.4 hotfix changes (Python reminder engine in `bot/reminders.py`, `AI_TIMEOUT` raised from 600s to 7200s for long R&D runs, `reminders_job` integration in `bot.py`) are part of v0.14.0. The hotfix had been merged on top of v0.13.0 on the main branch and is preserved unchanged by this release.

### Renamed
- **Control room renamed from "Command Bridge" to "Headquarters"** across the live runtime: `bot/i18n.py`, `bot/bot.py` boot text, `KAEL_SYSTEM_PROMPT` in `bot/config.py`, `ORCHESTRATOR.md`, `SCHEDULER.md`, `README.md`, and the docstring examples in `bot/messaging/base.py` / `discord.py` / `telegram.py`. Historical CHANGELOG entries and `releases/0.12.x.md` are intentionally left untouched as the record of the previous naming.
- **New migration `0.14.0-rename-command-bridge-to-headquarters`** in `bot/migrations.py`: runs once on the first boot after upgrade and renames the platform's main channel/topic to `Headquarters` (Telegram General topic) or slug `headquarters` (Discord/Slack). The historical `0.12.1-rename-main-to-command-bridge` is kept in the registry so fresh installs run both in registration order and end up correctly named in one boot.

### Added
- **Backend-aware model preferences** via new `models.yaml` at the repo root and new module `bot/model_preferences.py`. Workspaces, specialists, and tasks can now express intent as a semantic alias (`fast` / `balanced` / `powerful`) or as a role (`orchestrator` / `workspace` / `specialist` / `scheduled` / `one-shot`). `resolve_model_preference(model, backend, role)` looks the alias up in `aliases[<alias>][<backend>]` and returns the concrete model id understood by the active backend. Three layers of fallback: `models.yaml` → `AI_MODEL_DEFAULTS`/`AI_MODEL_ALIASES` env vars → hard-coded defaults — a brand-new clone with no `models.yaml` still boots cleanly. Legacy Claude-style names (`haiku`/`sonnet`/`opus`) are silently remapped to the semantic aliases so existing `tasks.md` and `specialists.md` rows keep working.
- **`Agent.model` field** persisted in `data/state.json`, propagated by `topics.create_workspace` / `topics.create_specialist` / `AgentManager.add_agent(model=...)`. Re-adding the same agent without a model arg preserves the previous preference, so `heal_detached_workspaces` cannot accidentally erase it.
- **Per-agent instructions on interactive turns**: `ai_invoke._load_agent_instructions(agent)` now appends the markdown brief from `agents/<name>.md` / `specialists/<name>.md` to `WORKSPACE_AGENT_SYSTEM_PROMPT` for every interactive turn. Previously only scheduled spawns saw the brief — interactive runs of the same agent gave generic answers.
- **OpenCode session persistence**: `OpenCodeBackend` now supports `--session <id>` resumption. `supports_sessions()` returns `True`; `can_resume_session(sid)` filters out KaelOps' generic UUIDs (only OpenCode's native `ses_…` ids are passed to the CLI); commands include `--format json` and `parse_response` walks NDJSON / single-blob payloads to extract `result` / `text` / `message.content` and recursively find `sessionID` / `sessionId` / `session_id` keys. Captured session ids are persisted on `agent.session_id` after every successful turn so conversations resume across messages and bot restarts.
- **`OpenCodeBackend._compose_message`**: OpenCode has no `--system-prompt` flag, so KaelOps now wraps the orchestrator system prompt inside `<system_instructions>` / `<user_message>` tags inside the user message itself.
- **`AIBackend.can_resume_session`** base method on the abstract backend interface; default implementation returns `bool(session_id)`. `AIBackend.parse_response` is now typed `str | dict[str, Any]` so backends can return either a plain text string (legacy) or `{text, session_id}` (OpenCode).
- **`_normalize_backend_response(parsed)`** in `bot/ai_invoke.py` hides the string-vs-dict difference from the rest of the invocation pipeline.
- **`_agent_model_role(agent)`** in `bot/ai_invoke.py` maps an `Agent` to its role key for `models.yaml` defaults (`kael` → `orchestrator`, `specialist` → `specialist`, everything else → `workspace`).
- **`topics.heal_detached_workspaces(manager, platform)`**: walks every live workspace, re-creates a topic for any agent whose `thread_id` is missing, persists the new id back into `tasks.md`, and posts a welcome message in the freshly attached topic. Hooked into Telegram boot through `boot_notify` *before* the boot message goes out.
- **`topics._update_table_thread_id` / `_update_task_thread_id` / `_update_specialist_thread_id`** helpers for rewriting the Thread ID column in `tasks.md` / `specialists.md` rows.
- **`bot.telegram_polling_kwargs()`**: centralised polling configuration with `timeout=10`, `read/write/connect/pool_timeout=15`, `poll_interval=1.0`, `bootstrap_retries=-1`, `drop_pending_updates=True`. Recovers Telegram polling within ~15 seconds of a macOS sleep/wake cycle instead of hanging for minutes.
- **Python 3.13 event loop guard** in `_run_telegram` so PTB still finds a default main-thread event loop even on CPython versions that no longer create one eagerly.
- **`PyYAML>=6.0`** in `bot/requirements.txt`. The bootstrap installer reinstalls deps automatically on the next boot.
- **74 new tests**, suite is now **725 passing** (from 651 baseline). Notable additions: `tests/test_model_preferences.py` (new file, 21 tests covering the resolver, role defaults, legacy mapping, fallback chain, YAML loader smoke tests); `TestRenameToHeadquartersMigration` + registration-order assertion in `tests/test_migrations.py`; `TestControlRoomId`, `TestSendMessageRawHttpx` in `tests/test_telegram_platform.py`; `TestSchedulerJobRoutesViaControlRoom`, `TestTelegramPollingKwargs` in `tests/test_bot.py`; `TestCreateWorkspacePersistsModel`, `TestUpdateTableThreadId`, `TestHealDetachedWorkspaces` in `tests/test_topics.py`; `TestNormalizeBackendResponse`, `TestAgentModelRole`, `TestLoadAgentInstructions` in `tests/test_ai_invoke.py`; expanded `TestOpenCodeBackend` covering `--format json`, NDJSON parsing, native session id filtering, system prompt inlining; `test_resolves_model_alias_via_model_preferences` in `tests/test_scheduler.py` and `test_dispatch_task_resolves_model_alias` in `tests/test_timed_scheduler.py`; new `Agent.model` round-trip and `add_agent(model=…)` semantics tests in `tests/test_agents.py`.

### Changed
- **`TelegramPlatform.control_room_id` returns `0`** (the General topic of a forum supergroup) instead of the hard-coded `1` that recent Bot API versions reject. Every scheduler / boot / update notification used to be silently dropped on Telegram forum chats — they now land in Headquarters again.
- **`Platform.control_room_id` widened to `int | None`** so future adapters with no thread concept can return `None` without violating the type.
- **`bot.py` sources every notification thread id from `plat.control_room_id`** instead of the old `TELEGRAM_MAIN_THREAD_ID = None` constant. `scheduler_job`, `update_check_job`, and `boot_notify` now route correctly across all three platforms with no platform-specific code paths.
- **`TelegramPlatform.send_message` bypasses python-telegram-bot** and POSTs directly to the Bot API via `httpx`. PTB's `Bot.send_message` was intermittently unreliable for control-room sends in forum chats (silent drops, occasional 60 s hangs after sleep/wake). Failure mode is now predictable and easy to time out (30 s).
- **`bot.py` uses `telegram_polling_kwargs()`** in `_run_telegram` instead of the bare `drop_pending_updates=True, allowed_updates=Update.ALL_TYPES` it used to pass.
- **`scheduler.spawn_task` and `timed_scheduler.dispatch_task`** both route the queued model through `resolve_model_preference(task.get("model"), backend, role=task.get("type"))` so semantic aliases become concrete backend ids before reaching the CLI. Previously a row with `model="balanced"` was passed to OpenCode literally and rejected.
- **`ai_invoke._invoke_ai_locked` resolves `model or agent.model`** against the agent's role default. Previously the default was `"sonnet"` regardless of backend, which meant Codex / OpenCode users got an unknown model id.
- **OpenCode session id only reused when valid**: `_invoke_ai_locked` now consults `backend.can_resume_session(agent.session_id)` before passing the id to `build_command`. KaelOps' UUID is filtered out for backends that need their own format.
- **Boot notification on Telegram now calls `heal_detached_workspaces`** before sending the boot message, so the boot summary reflects freshly-attached channels.
- **System prompt** in `KAEL_SYSTEM_PROMPT` updated: `[CREATE_WORKSPACE]` and `[CREATE_SPECIALIST]` model attribute documented as `<fast|balanced|powerful or explicit model id>` instead of the legacy `<haiku|sonnet|opus>`. `ORCHESTRATOR.md` mirrors the change.

## 0.13.0

### Added
- **Timed Task Queue** (`bot/timed_scheduler.py`): high-frequency scheduler (default 60 s) reading `data/timed_queue.json`. Two-tier architecture — periodic scheduler keeps `tasks.md` + 10 min loop for infrastructure agents, timed queue handles one-shot and dynamic periodic tasks created at runtime by agents. Atomic write-then-rename for race safety, jitter/offline recovery for missed events, automatic startup migration of one-shot rows from `tasks.md`.
- **`TIMED_SCHEDULER_INTERVAL`** env var (default `60`).

## 0.12.3

### Changed
- Documentation refresh: `README.md`, `ORCHESTRATOR.md`, and `SCHEDULER.md` brought in sync with the v0.11.x → v0.12.x feature line. No code changes.
- `README.md`: platform-agnostic tagline and intro; new "Command Bridge" concept documented in "How It Works" and "Kael — The Orchestrator"; coordination-first contract written explicitly; new "Receiving Images" section documenting `[SEND_IMAGE]`; configuration section expanded from a single Telegram-only table to four (Common, Telegram, Discord, Slack); "Telegram Commands" renamed to "Commands" with cross-platform note; Auto-Updates section rewritten to cover `apply_update` hardening, `_bootstrap.py` safety net, and the migration framework; workspace creation flow genericized ("topic/channel" instead of "Telegram topic"); Project Structure updated with `_bootstrap.py`, `media.py`, `migrations.py`, `messaging/` subpackage, `migrations.json` tracker, and 630+ test count.
- `ORCHESTRATOR.md`: Kael now "lives on the Command Bridge" everywhere; intro mentions all three platforms; coordination-first contract added; rule #2 updated; new rule #3 forbidding project-specific execution from the Bridge.
- `SCHEDULER.md`: responsibility #7 and the corresponding rule now reference the Command Bridge instead of "Main channel" / "Telegram notifications".

## 0.12.2

### Fixed
- **Auto-update now reliably reinstalls Python dependencies.** Root cause of "No module named 'PIL'" after upgrading to v0.12.0: `apply_update`'s pip install step ran with `-q`, ignored the return code, threw away stdout/stderr, and had a 120 s timeout. A silently-failed install was reported as success, so the bot rebooted on new code against a stale venv. Now: verbose pip output, return code checked, stdout/stderr logged at INFO, 600 s timeout, rollback + clear error on failure, preflight check that `.venv/bin/pip` exists.
- **New startup safety net `bot/_bootstrap.py`.** Hash-based dependency check that runs at the top of `bot/bot.py` before any other import. Reruns `pip install -r requirements.txt` whenever `requirements.txt` changed since the last successful install (hash stored at `.venv/.kaelops_deps_hash`). Covers the cases the updater alone cannot: manual pull without pip install, crashed update between `git pull` and pip step, manually-touched venv between restarts. Uses only the stdlib, is idempotent, and is a fast no-op on the common path.
- On successful auto-update, `apply_update` now refreshes `.venv/.kaelops_deps_hash` so the boot that follows does not redundantly re-run pip for the same requirements.

### Added
- 11 new tests: `tests/test_bootstrap.py` (7 — new file), `tests/test_updater.py` (+4 for pip nonzero rollback, pip timeout rollback, missing pip binary, marker refresh on success). Suite is now 632 tests.

## 0.12.1

### Added
- **Migration framework** (`bot/migrations.py`): post-update instructions that run exactly once per deployment on the next boot after an update. Each migration is a registered async function tracked in `data/migrations.json`. Never retries a migration that was recorded as `failed` or `error`, so the bot is never blocked at boot by an unsatisfiable migration. Runner hooked into Telegram `boot_notify`, Discord `on_ready`, and Slack `_run`.
- **`Platform.rename_main_channel(display_name, slug)`** abstract method with implementations for all three adapters:
  - Telegram via `Bot.edit_general_forum_topic` (uses display name)
  - Discord via `channel.edit(name=slug)` with idempotency check
  - Slack via `conversations_rename` with `conversations_info` idempotency check
- **First migration**: `0.12.1-rename-main-to-command-bridge` renames the platform's main destination to "Command Bridge" on first boot after upgrade.
- 20 new tests: `tests/test_migrations.py` (10), `tests/test_telegram_platform.py` (4 — new file), plus additions in Discord and Slack platform tests. Suite is now 621 tests.

### Changed
- **`KAEL_SYSTEM_PROMPT` rewritten** to introduce an explicit "Command Bridge" contract. Kael now treats the control channel as a coordination-only space: fleet status, workspace creation, delegation, meta-operations are in scope; project-specific work (R&D iterations, builds, deploys, feature implementation) is NOT — Kael delegates via `[DELEGATE]` or redirects to the workspace topic instead of executing directly on the Bridge.
- **Boot notification** text changed from `"KaelOps vX.Y.Z started."` to `"*KaelOps vX.Y.Z* — Command Bridge online."` across all three platforms. If any migrations were executed this boot, they are listed inline in the boot message.
- **User-visible strings** in `bot/i18n.py` updated: `help_text`, `no_workspaces`, `unmapped_topic`, and `focus_off` now reference the Command Bridge instead of "main channel" / "Main".
- Top-of-file docstring in `bot/bot.py` updated to describe Kael as living on the Command Bridge.

## 0.12.0

### Added
- **Outgoing images**: agents can attach a photo to their reply via a new `[SEND_IMAGE path="..." caption="..."]` response pattern. Multiple images per response supported. System prompt instructs agents to only emit this command on explicit user request — never proactively.
- `bot/media.py` with `prepare_image_for_upload(path, max_bytes)`: re-encodes images as JPEG with a quality sweep (90 → 40) and progressive downscaling (100% → 25%) until the file fits under the platform's upload cap. Raises `MediaError` for missing files, non-images, or unfittable files.
- `Platform.send_photo(chat_id, path, caption, thread_id)` abstract method, with implementations for Telegram (`sendPhoto`), Discord (`channel.send(file=...)`), and Slack (`files_upload_v2`). Failures return `None` and are logged rather than raised.
- `Platform.max_photo_bytes` property with per-adapter override (Telegram 10 MiB, Discord 8 MiB, Slack 1 GiB).
- `_handle_media_commands` in `bot/handlers.py` runs inside `_process_and_send` and routes `[SEND_IMAGE]` patterns to `platform.send_photo`, stripping them from the user-visible text and appending inline error notices on failure.
- `Pillow>=10.0` added to `bot/requirements.txt`.
- 20 new tests across `test_media.py`, `test_handlers.py`, `test_discord_platform.py`, `test_slack_platform.py`. Suite is now 601 tests.

## 0.11.2

### Fixed
- **Endless "typing…" on session-ID collision**: agents carrying placeholder session ids (e.g. `00000000-0000-0000-0000-000000000003`) would be rejected by the Claude CLI with "already in use", and the retry branch reused the same id so the loop could never recover. Added `AgentManager._load_state` sanitisation that regenerates any placeholder / non-UUID session id as a fresh `uuid.uuid4()` and resets session progress. The retry path in `_invoke_ai_locked` now regenerates the session id too.
- **`AI_TIMEOUT` lowered from 3600s to 600s** so hung CLI invocations surface within 10 minutes instead of an hour.

### Added
- `_is_placeholder_session_id` helper in `bot/agents.py`.
- Tests: `TestPlaceholderSessionIdSanitisation`, `TestIsPlaceholderSessionId`, and `test_session_collision_regenerates_session_id`. Suite is now 581 tests.

## 0.11.1

### Fixed
- **Topic routing**: messages posted in a forum topic / channel that is not bound to any workspace agent no longer silently migrate the conversation to the main channel. The bot now replies in-place with an explicit hint and does not invoke the AI. This was the root cause of "Kael stops typing in the topic and typing suddenly appears in #general".
- **Discord / Slack main-channel routing**: the legacy `MAIN_THREAD_ID = 1` sentinel made Kael's main destination unreachable on Discord (channel lookup `1` always failed) and Slack (`thread_ts="1"` is invalid). Replaced with a new `Platform.is_main_thread(chat_id, thread_id)` abstraction implemented per adapter.
- **Silent error swallowing**: `_process_and_send` now routes error messages through a `_safe_send` helper that falls back to plain text if markdown rendering fails, so the user always sees what went wrong.
- **Empty AI response**: `_send_response` no longer sends just the agent tag when the response becomes empty after pattern stripping.
- **Workspace creation batch**: a single failing `create_workspace` / `create_specialist` inside a multi-create response no longer aborts the whole batch.
- **Keep-alive task cleanup**: `invoke_ai` now awaits the keep-alive task after cancelling it in `finally`, avoiding orphaned typing requests and "coroutine was never awaited" warnings.

### Added
- `Platform.is_main_thread(chat_id, thread_id)` abstract method with per-platform implementations (Telegram, Discord, Slack).
- `STRINGS["unmapped_topic"]` i18n entry.
- Tests for the new routing behaviour and for `is_main_thread` on each adapter. Suite is now 576 tests.

## 0.11.0

### Added
- Non-interactive setup via CLI flags for scripted / headless installs.
- Discord setup auto-detection (server, channel, owner).
- Telegram diagnostics for raw updates, inbound messages, and privacy mode warnings during startup.
- Detailed setup guidance in README and prompts for all three platforms.
- Uninstall instructions with keep-alive warnings.

### Changed
- Updated OpenCode execution to use `opencode run` and only pass explicit provider-qualified model names.
- Stopped forcing Telegram main-room replies onto thread `1`, keeping main-thread handling aligned with current platform behavior.
- Updated install scripts to select the newest available Python 3.10+ interpreter and recreate `.venv` with `--clear`.

### Fixed
- Scheduler log now uses UTC timestamps.
- Adjusted tests to cover the revised OpenCode model handling and Telegram main-thread behavior.
