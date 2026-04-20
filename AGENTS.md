# Robyx Agent Notes

## Entrypoints
- Main runtime entrypoint is `bot/bot.py`.
- `bot/handlers.py` owns command/message routing.
- `bot/topics.py` is the write path for workspace/specialist/continuous-task creation and mutates `data/queue.json`, `data/specialists.md`, and instruction files under `data/agents/` and `data/specialists/`. These paths live under `data/` (gitignored) since v0.16; the repo ships a clean shell with no personal runtime data.
- `bot/scheduler.py` is the unified scheduler (every 60s). It reads `data/queue.json` and handles all task types: reminders, one-shot, periodic, and continuous. Agents schedule work via `scheduler.add_task(...)`.
- `bot/continuous.py` manages state for continuous (iterative autonomous) tasks. State lives in `data/continuous/<name>/state.json`.
- `bot/task_runtime.py` resolves agent identity and `work_dir` for scheduled/timed runs so they execute with the correct context and memory.
- `bot/scheduled_delivery.py` relays parsed AI output from scheduled runs back into the target workspace/specialist topic.
- `bot/lifecycle_macros.py` parses & dispatches workspace-scoped lifecycle macros emitted by the primary workspace agent (`[LIST_TASKS]`, `[TASK_STATUS]`, `[STOP_TASK]`, `[PAUSE_TASK]`, `[RESUME_TASK]`, `[GET_PLAN]`). Contract at `specs/005-unified-workspace-chat/contracts/lifecycle-macros.md`.
- `bot/update_plan_macro.py` handles `[UPDATE_PLAN]` — partial in-place merge of a continuous task's program (`objective`, `success_criteria`, `constraints`, `checkpoint_policy`, `context`, `plan_text`). Workspace-scoped; unknown fields ignored for forward compatibility.
- `bot/config_updates.py` intercepts `KEY=value` messages in `handlers.py` and applies them directly to `.env` without routing secrets through the AI backend.

## Commands
- Install runtime deps: `.venv/bin/pip install -r bot/requirements.txt`
- Install test deps: `.venv/bin/pip install -r tests/requirements-test.txt`
- Run locally: `.venv/bin/python bot/bot.py`
- Run full test suite from repo root: `pytest`
- Run focused tests: `pytest tests/test_scheduler.py -k <expr>`
- Non-interactive setup for agent-driven installs: `python3 setup.py --backend <claude|codex|opencode> --platform <telegram|slack|discord> ... -y`

## Config Gotchas
- `bot/config.py` loads `.env` at import time and exports module-level constants. In tests or scripts, patch env/config before importing modules that do `from config import ...`.
- If the backend binary is not on `PATH`, set `AI_CLI_PATH` in `.env`.
- `OpenCodeBackend` only forwards `--model` when the model string contains `/`; plain names like `sonnet` are ignored for OpenCode runs.

## File Format Contracts
- `data/queue.json` is the unified task queue (reminders, one-shot, periodic, continuous). All entries share an atomic claim system.
- `data/specialists.md` is a machine-parsed Markdown table. Preserve pipe-table structure and column order when editing manually.
- `data/continuous/<name>/state.json` tracks iterative task progress (steps, history, next planned step).
- Legacy formats (`data/tasks.md`, `data/timed_queue.json`, `data/reminders.json`) are migrated automatically at boot into `queue.json`.

## Changelog
- Maintain `CHANGELOG.md` for teammate-facing project updates.
- Add new entries under `## Unreleased` and keep the newest relevant changes there until they ship.
- Keep entries short, factual, and easy to scan in reviews.
- Prefer `### Added`, `### Changed`, and `### Fixed` sections when they fit.
- Record meaningful behavior, workflow, install, or developer-experience changes; skip trivial internal-only edits.

## Testing Conventions
- Run tests from the repo root. `tests/conftest.py` prepends `bot/` to `sys.path`, so imports assume that working directory.
- When adding tests, patch both `config` and any module-level copies imported with `from config import ...`; the existing fixtures show the pattern.

## Service Workflow
- `install/install-mac.sh` and `install/install-linux.sh` recreate `.venv` with `--clear`, reinstall deps, and run `setup.py` if `.env` is missing.
- Prefer `install/uninstall-mac.sh` or `install/uninstall-linux.sh` to stop/remove the service. Do not just kill the bot process: launchd/systemd will respawn it.

## Memory Behavior
- Workspace memory is a single SQLite file at `<work_dir>/.robyx/memory.db` (not a directory). See `bot/memory_store.py::db_path_for_agent` for the exact mapping.
- Orchestrator and specialists are centralized instead: `data/memory/robyx.db` and `data/memory/<specialist>.db`.
- If a workspace project already has `CLAUDE.md` or a non-empty `.claude/`, Robyx does not create a `.db` for that workspace — it defers to the project's native memory.

## Agent Session Lifecycle (since v0.15.1, fixed in v0.15.2)
- The Claude Code CLI bakes the system prompt at session creation and ignores `--append-system-prompt` on `--resume`. Any change to a system prompt (`bot/config.py`) or per-agent brief (`data/agents/<name>.md`, `data/specialists/<name>.md`) is invisible to existing sessions until the session is regenerated.
- `bot/updater.py:apply_update` handles this automatically: after a successful pull it diffs `<pre>..HEAD`, identifies which agents were affected (per `bot/session_lifecycle.py:GLOBAL_INVALIDATION_FILES` and the `agents/`/`specialists/` path patterns), and calls `AgentManager.reset_sessions(targets)` before the restart.
- **The reset MUST go through `AgentManager.reset_sessions`.** Never write to `data/state.json` directly: the running bot holds the pre-mutation copy in memory and the next `save_state()` call will silently overwrite your direct write. This is the bug that made v0.15.0 and v0.15.1 ineffective in production. The regression is locked in by `tests/test_agents.py::TestResetSessionsSurvivesSubsequentSaveState::test_direct_state_json_mutation_would_be_clobbered` which **demonstrates** the failure pattern; if it ever starts failing, the underlying assumption changed and the test needs to be revisited.
- **Do not write per-release migrations for prompt or brief changes.** The updater contract covers them. Migrations remain the right tool for structural data changes (renames, schema bumps, channel renames), not for "I touched a prompt". When you do write a migration, remember its signature is now `async def my_migration(platform, manager) -> bool` — channel-rename migrations can ignore `manager`, state-mutating migrations call `manager.reset_sessions(...)`.
- If you add a new file whose contents must invalidate sessions when changed (e.g. a new system-prompt module), add its repo-relative path to `bot/session_lifecycle.GLOBAL_INVALIDATION_FILES` and add a test in `tests/test_session_lifecycle.py:TestAgentsToInvalidate`.
