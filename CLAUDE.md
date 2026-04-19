# robyx-ai Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-04-19

## Active Technologies
- Python 3.10+ + python-telegram-bot, discord.py, slack-sdk, python-dotenv, PyYAML, Pillow (002-full-code-review)
- JSON files under `data/`, SQLite for memory (new in 0.21.0) (002-full-code-review)
- JSON under `data/` + SQLite (`data/memory.db`) since v0.21.0 (002-full-code-review)
- Python 3.10+ + python-telegram-bot (for `ChatMemberHandler`), existing internal modules (`bot/agents.py`, `bot/collaborative.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/messaging/*`) (003-external-group-wiring)
- `data/collaborative_workspaces.json` (existing atomic JSON store with fcntl/msvcrt locking, `_write_unlocked` via temp-file + `os.replace`); agent instructions at `data/agents/<name>.md`. No schema change to CollabWorkspace; reuse existing `parent_workspace`, `inherit_memory`, `status`, `expected_creator_id` fields. (003-external-group-wiring)
- Python 3.10+ + python-telegram-bot, discord.py, slack-sdk; stdlib `re`, `json`, `pathlib`, `logging` (004-fix-continuous-task-macro)
- JSON state under `data/continuous/<name>/state.json` (existing; unchanged) (004-fix-continuous-task-macro)
- Python 3.10+ + python-telegram-bot, discord.py, slack-sdk (via existing `bot/messaging/*` adapters); internal modules `bot/scheduler.py`, `bot/continuous.py`, `bot/continuous_macro.py`, `bot/topics.py`, `bot/scheduled_delivery.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/migrations/*` (005-unified-workspace-chat)
- JSON files under `data/` — `data/queue.json` (scheduler queue, atomic write-then-rename with `fcntl` locking), `data/continuous/<name>/state.json` (per-task state), `data/continuous/<name>/plan.md` (new, per-task plan artifact) (005-unified-workspace-chat)

- Python 3.10+ + sqlite3 (stdlib), optionally sqlite-vec (~165KB) (001-memory-engine-analysis)

## Project Structure

```text
src/
tests/
```

## Commands

cd src && pytest && ruff check .

## Code Style

Python 3.10+: Follow standard conventions

## Recent Changes
- 005-unified-workspace-chat: Added Python 3.10+ + python-telegram-bot, discord.py, slack-sdk (via existing `bot/messaging/*` adapters); internal modules `bot/scheduler.py`, `bot/continuous.py`, `bot/continuous_macro.py`, `bot/topics.py`, `bot/scheduled_delivery.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/migrations/*`
- 004-fix-continuous-task-macro: Added Python 3.10+ + python-telegram-bot, discord.py, slack-sdk; stdlib `re`, `json`, `pathlib`, `logging`
- 003-external-group-wiring: Added Python 3.10+ + python-telegram-bot (for `ChatMemberHandler`), existing internal modules (`bot/agents.py`, `bot/collaborative.py`, `bot/handlers.py`, `bot/ai_invoke.py`, `bot/messaging/*`)


<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
