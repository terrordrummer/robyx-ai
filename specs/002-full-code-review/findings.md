# Code Review Findings: Robyx

**Date**: 2026-04-16
**Baseline**: 1089 tests passed, 12,298 LOC
**Final**: 1085 tests passed (4 tests for dead code removed), 12,329 LOC

## Bug Findings (US1) — All Fixed

| ID | Module | Sev | Description | Fix |
|----|--------|-----|-------------|-----|
| F01 | updater.py | High | `extractall` without symlink/hardlink check | Added `issym()`/`islnk()` rejection |
| F02 | bot.py | Med | Double shutdown on signal + atexit | `_shutdown_done` guard |
| F03 | handlers.py | Med | `notes["min_compatible"]` when notes is None | None guard |
| F04 | updater.py | Med | Pip failure rollback skips data snapshot restore | Added restore to 3 paths |
| F05 | scheduler.py | Med | Reminder send catches only OSError/RuntimeError | Widened to `except Exception` |
| F06 | ai_invoke.py | Med | `read_text()` race after `stat()` | try/except with cache eviction |
| F07 | ai_invoke.py | Low | orphan_tracker in finally could skip cleanup | try/except/finally |
| F08 | handlers.py | Med | Double `get_by_thread` + side-effect pattern | Local variable |
| F09 | telegram.py | High | `_bot` not initialized in `__init__` | `self._bot = None` |
| F10 | topics.py | High | `platform=None` default causes AttributeError | Early None guard |
| F11 | orphan_tracker.py | High | `kept` dict never populated — clears registry | `kept[pid_str] = meta` |
| F15 | scheduled_delivery.py | Med | `read_text()` crash on non-UTF-8 | `errors="replace"` |
| F18 | config.py | Med | `int()` on env vars no ValueError guard | `_int_env()` helper |
| F19 | memory.py | Low | Double-brace placeholder mismatch | Fixed template |
| F21 | topics.py | Med | `close_workspace` crashes when platform is None | Guard added |

## Dead Code Removed (US2)

| Item | Module | What |
|------|--------|------|
| D1 | process.py | `is_bot_process()` async — never called |
| D7 | collaborative.py | `InteractionMode` enum — never used |
| D3-D5 | config.py | `ORCHESTRATOR_MD`, `SCHEDULER_MD`, `STATUS_INTERVAL` — never imported |
| D10 | topics.py | `_update_table_thread_id()`, `_update_specialist_thread_id()` — never called |

Additional dead items identified but kept (legacy fallback): memory.py legacy functions (save_active, append_archive, load_active, load_archive_index, word_count, is_over_budget).

## Security Findings (US3)

| ID | Module | Sev | Description | Fix |
|----|--------|-----|-------------|-----|
| S1 | handlers.py | Med | AI-controlled `work_dir` for continuous tasks not validated | Added WORKSPACE path validation |
| S2 | config_updates.py | Med | Security-critical keys (BOT_TOKEN, OWNER_ID) changeable via chat | Removed from KNOWN_ENV_KEYS |
| S3 | discord.py | Med | `download_voice` SSRF — fetches arbitrary URLs | Added domain validation |

**Confirmed clean**: command injection (no shell=True), token logging, privilege escalation, SQL injection, tarball extraction, image path validation.

## Performance Findings (US4) — Documented, Deferred

| ID | Module | Description | Status |
|----|--------|-------------|--------|
| P1 | scheduler.py | Redundant queue.json read in continuous task handler | Deferred — design change |
| P2 | scheduler.py | Blocking sync file I/O in async functions (os.fsync) | Deferred — needs asyncio.to_thread |
| P3 | scheduler.py | O(n) linear scan in reconciliation | Deferred — build index dict |
| P4 | agents.py | save_state() called too frequently | Deferred — needs debounce design |
| P5 | scheduler.py | Template re-read without cache | Deferred — trivial, low priority |

## Deferred Findings (need design decisions)

| ID | Module | Sev | Description |
|----|--------|-----|-------------|
| F12 | telegram.py | Med | `send_to_channel` unconditional Markdown — behavioral change risk |
| F13 | discord.py | Med | `download_voice` error handling incomplete |
| F14 | slack.py | Med | `reply`/`edit_message` no error handling |
| F17 | collaborative.py | Med | Partial load on malformed JSON |
| F20 | voice.py | Med | `%` formatting can raise TypeError |
| All adapters | Med | `reply`/`edit_message` unprotected while `send_message` uses retry_send |

---

## Pass 2 Baseline (recorded 2026-04-16, T059)

| Metric | Value |
|--------|-------|
| Tests (pytest) | 1085 passed, 1 skipped (1086 collected) |
| LOC under `bot/` | 12 329 |
| Modules under `bot/` | 53 |
| Migration files (`bot/migrations/v*.py`) | 18 (`v0_20_12` … `v0_21_0`) — plan.md erroneously said 25 |
| Version | 0.21.0 |
| Plan-assumption correction | `bot/i18n.py` is **single-locale English**, not IT+EN. Pass 2 plan and conversation contract assumed bilingual — to correct before Phase 12. |

## Pass 2 Findings

| ID | Module | Lens | Sev | Description | Fix | Status |
|----|--------|------|-----|-------------|-----|--------|
| P2-00 | plan.md / contract | Meta | Low | Pass 2 planning docs claimed IT/EN locale parity, but `bot/i18n.py` is English-only | Corrected in plan.md + contracts/conversation-contract.md; single-locale discipline now articulated; multi-locale becomes a *future* concern | fixed |
| P2-01 | handlers.py:213 | NI | Low | Hard-coded literal `"Usage: /reset <name>"` passed to `platform.reply` (bypasses i18n) | Relocate to `bot/i18n.py` under key `reset_usage`; replace call site with `t("reset_usage")` | open |
| P2-02 | handlers.py:306 | NI | Low | Hard-coded literal `"Checking for pending update..."` passed to `platform.reply` | Relocate to i18n key `update_checking_manual`; reuse existing `update_checking` if semantics match | open |
| P2-03 | handlers.py:1350 | NI | Low | Hard-coded literal `"No users registered in this workspace."` passed to `platform.reply` | Relocate to i18n key `collab_no_users`; add i18n key and swap call | open |

