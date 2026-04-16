# Code Review Findings: Robyx

**Date**: 2026-04-16
**Baseline**: 1089 tests passed, 12,298 LOC

## Findings

| ID | Module | Category | Severity | Description | Fix |
|----|--------|----------|----------|-------------|-----|
| F01 | updater.py | SEC | High | `extractall` without symlink/hardlink check — crafted tarball could use symlinks to write outside DATA_DIR | Added `issym()`/`islnk()` rejection before `extractall` |
| F02 | bot.py | BUG | Medium | Signal handler calls `save_on_exit()` which also runs via `atexit`, causing double state save | Added `_shutdown_done` guard to prevent double execution |
| F03 | handlers.py | ERR | Medium | `notes["min_compatible"]` accessed when `notes` can be None in `cmd_checkupdate` | Added None guard with `"unknown"` fallback |
| F04 | updater.py | BUG | Medium | Pip failure/timeout/missing rollback restores code but not data snapshot — leaves data in partially-migrated state | Added `_restore_data_dir(snapshot)` to all 3 pip failure paths |
| F05 | scheduler.py | ERR | Medium | Reminder send catches only `OSError`/`RuntimeError` — unexpected platform exceptions kill the dispatch loop | Widened to `except Exception` |
| F06 | ai_invoke.py | BUG | Medium | `path.read_text()` after `stat()` can race (file deleted between calls) causing unhandled `OSError` | Wrapped in try/except with cache eviction |
| F07 | ai_invoke.py | ERR | Low | `orphan_tracker.unregister()` in finally block could raise, skipping `agent.running_proc = None` | Wrapped in try/except/finally to guarantee cleanup |
| F08 | handlers.py | BUG | Medium | `manager.get_by_thread(thread_id)` called twice + `log.warning() or "robyx"` side-effect pattern | Extracted to local variable with explicit conditional |
