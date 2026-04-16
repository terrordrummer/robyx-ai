# robyx-ai Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-04-16

## Active Technologies
- Python 3.10+ + python-telegram-bot, discord.py, slack-sdk, python-dotenv, PyYAML, Pillow (002-full-code-review)
- JSON files under `data/`, SQLite for memory (new in 0.21.0) (002-full-code-review)
- JSON under `data/` + SQLite (`data/memory.db`) since v0.21.0 (002-full-code-review)

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
- 002-full-code-review: Added Python 3.10+ + python-telegram-bot, discord.py, slack-sdk,
- 002-full-code-review: Added Python 3.10+ + python-telegram-bot, discord.py, slack-sdk, python-dotenv, PyYAML, Pillow

- 001-memory-engine-analysis: Added Python 3.10+ + sqlite3 (stdlib), optionally sqlite-vec (~165KB)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
