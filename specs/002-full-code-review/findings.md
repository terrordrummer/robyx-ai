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
| F17 | collaborative.py | Med | Partial load on malformed JSON — **closed by P2-30 (see Pass 2 Findings)** |
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

### Baseline refreshed 2026-04-18 (after rebase onto main, 003/004 merged via v0.22.1)

| Metric | Start (2026-04-16) | Refreshed (2026-04-18) | Delta |
|--------|--------------------|------------------------|-------|
| Tests (pytest) | 1086 collected | 1451 collected | +365 |
| LOC under `bot/` | 12 329 | 14 576 | +2 247 |
| Modules under `bot/` | 53 | 59 | +6 |
| Migration files | 18 (`v0_20_12` … `v0_21_0`) | 23 (`v0_20_12` … `v0_22_1`) | +5 (`v0_21_1`, `v0_21_2`, `v0_21_3`, `v0_22_0`, `v0_22_1`) |
| Version | 0.21.0 | 0.22.1 | +0.1.1 |
| New modules added | — | `bot/continuous_macro.py` (704 LOC), migrations `v0_22_0.py` + `v0_22_1.py`, + 5 new test modules | — |
| Audit targets modified by 003/004 | — | `bot/bot.py`, `bot/authorization.py`, `bot/messaging/base.py`, `bot/messaging/telegram.py`, `bot/messaging/discord.py`, `bot/messaging/slack.py`, `bot/collaborative.py`, `bot/handlers.py`, `bot/i18n.py`, `bot/ai_invoke.py`, `bot/scheduled_delivery.py` | — |

Close-out gate threshold (T117) correspondingly lifted from ≥ 1086 to ≥ 1451. New SEC task **T079a** added in `tasks.md` for `bot/continuous_macro.py`. **T112** (Pass 1 F17) marked closed — T085 already addressed it.

## Pass 2 Findings

| ID | Module | Lens | Sev | Description | Fix | Status |
|----|--------|------|-----|-------------|-----|--------|
| P2-00 | plan.md / contract | Meta | Low | Pass 2 planning docs claimed IT/EN locale parity, but `bot/i18n.py` is English-only | Corrected in plan.md + contracts/conversation-contract.md; single-locale discipline now articulated; multi-locale becomes a *future* concern | fixed |
| P2-01 | handlers.py:213 | NI | Low | Hard-coded literal `"Usage: /reset <name>"` passed to `platform.reply` (bypasses i18n) | Relocated to `STRINGS["reset_usage"]`; call site updated. | **fixed** |
| P2-02 | handlers.py:306 | NI | Low | Hard-coded literal `"Checking for pending update..."` passed to `platform.reply` | Relocated to `STRINGS["update_checking_manual"]`; call site updated. | **fixed** |
| P2-03 | handlers.py:1350 | NI | Low | Hard-coded literal `"No users registered in this workspace."` passed to `platform.reply` | Relocated to `STRINGS["collab_no_users"]`; call site updated. Ripgrep confirms zero remaining direct-literal violations across the 105 messaging call-sites. | **fixed** |
| P2-10 | messaging/slack.py:172 | Security | **High** | `download_voice` forwarded the bot token across 3xx redirects via `follow_redirects=True`, with no host allow-list on `file_id`. A hostile redirect could exfiltrate the Slack bot token. | Added `_validate_slack_file_url` guard (HTTPS + Slack-hostname allow-list). `download_voice` now disables automatic redirect following and re-validates every Location before replaying the Authorization header. | **fixed** |
| P2-20 | bot.py:108 | Stability | **High** | `ensure_single_instance` used a TOCTOU pattern (`if PID_FILE.exists(): read; else: write`). Two processes starting within the race window could both pass the check and both run — the single-instance promise was advisory at best. | Replaced with a POSIX `fcntl.LOCK_EX \| LOCK_NB` advisory lock on a sidecar `bot.pid.lock` file. The lock fd is held for the lifetime of the process; kernel releases it on exit (even on SIGKILL), so stale PID files no longer keep the lock held. Non-POSIX platforms fall back to the legacy check. | **fixed** |
| P2-11 | messaging/discord.py:170 | Security | Med | `download_voice` read the entire attachment body into memory via `resp.read()` without a size cap. A hostile redirect or crafted event could point the bot at a multi-GB payload and OOM the process. | Switched to streaming with `resp.content.iter_chunked(64 KB)`, capped at `_MAX_DISCORD_DOWNLOAD_BYTES = 25 MB`. Content-Length header also short-circuited when it declares > 25 MB. | **fixed** |
| P2-12 | messaging/discord.py:159 | Security | Med | The HTTPS + Discord-hostname allow-list from Pass 1 (S3) was inline in `download_voice` only. Any future download path would have to re-implement it, risking an SSRF regression. | Factored into module-level `_validate_discord_url()` with the allow-list expanded to include `discordapp.net` (Discord's media CDN). Called from every HTTP fetch path. | **fixed** |
| P2-30 | agents.py:151 + collaborative.py:170 | Stability | Med | A malformed `state.json` or `collaborative_workspaces.json` triggered a broad `except` that logged a warning and continued with empty in-memory state. The NEXT write would overwrite the corrupt file, destroying the original bytes forever. | Both load paths now: (a) catch `json.JSONDecodeError` and `UnicodeDecodeError` separately from other exceptions, (b) call new `_quarantine_corrupt_file(path, reason)` to rename the bad file to `*.corrupt-<UTC-timestamp>` before continuing. Operators keep the forensic evidence; the next save creates a fresh file. **Closes Pass 1 F17.** | **fixed** |
| T072 | messaging/slack.py | Security | — | Follow-up items for Slack adapter: event-ID dedup store bound + error-path token scrubbing | **Re-scoped to 'no action'**: event dedup is handled by `slack-bolt` library (not our code); `_bot_token` only appears in the `download_voice` Authorization header, never echoed in logs. No residual gap. | noted |
| P2-40 | migrations/tracker.py:51 | Stability | Med | `save(data_dir, tracker)` used plain `path.write_text(json.dumps(...))` with no tmp-file, no `fsync`, no atomic rename. A SIGKILL or power loss mid-write could leave `migrations.json` partially serialised — on next boot `load()` treats it as empty and **every migration in the chain re-runs** (safe only if every step is strictly idempotent). | Rewritten to go through `tmp + fsync + os.replace`. `fsync` failures are swallowed (tmpfs / non-durable filesystems) but the rename is still atomic. Test simulates a replace-step failure and verifies the original file is byte-identical afterwards. Closes C2 from `crash-matrix.md`. | **fixed** |
| T074 | config_updates.py | Security | — | Trust-boundary X-3: `.env` hot-reload should be guarded by a mutex so secrets don't rotate mid-AI-invocation | **Re-scoped to 'no action'**: there is **no hot-reload mechanism** in the codebase. `config_updates.py` writes `.env`; the bot reads it once at startup via `load_dotenv()`. User-facing i18n strings (`voice_no_key`) explicitly tell users to restart the bot after editing `.env`. Trust-boundary X-3 was mis-identified. | noted |
| T080 | scheduler.py | Stability | Med | Crash-matrix C6: scheduler intervals use wall-clock; an NTP jump can cause double-fire or missed-fire of retries and stale-claim resets | **Deferred with rationale**: after reading the code, `scheduler.py` uses `datetime.now(timezone.utc)` (not `time.time()`) and **most usages are legitimately wall-clock** — serialised `created_at`/`sent_at`/`canceled_at` timestamps, absolute `fire_at` deadlines, recurring-task `run_at` advancement. A correct fix would require threading monotonic references through in-memory state for interval-only computations (stale-claim TTL, reminder-age checks) while keeping wall-clock for serialisation and user-visible deadlines — substantially larger than one slice. Needs its own spec. | deferred |
| P2-50 | media.py:71 | Security | Med | Pillow's default `MAX_IMAGE_PIXELS` (89 MP) raises `DecompressionBombWarning` — NOT an error, so a crafted image in that range would still be decoded. No pre-Pillow file-size cap either, so a 10 GB crafted image would reach the decoder before any cap kicked in. Trust-boundary TG-4. | Added `_MAX_IMAGE_FILE_BYTES = 25 MB` on-disk ceiling enforced before `Image.open`; lowered `Image.MAX_IMAGE_PIXELS` to 50 MP; wrapped the open/load in `warnings.catch_warnings()` + `simplefilter('error', DecompressionBombWarning)` so any suspicious image raises `MediaError` instead of silently allocating. Handles both the `DecompressionBombWarning` warning zone and the hard `DecompressionBombError` (`> 2 × MAX_IMAGE_PIXELS`). | **fixed** |
| P2-60 | i18n/handler parity | NI | Low | No automated test verified that every registered `/command` appears in `help_text` and vice versa. Similarly, no test caught unsubstituted `{placeholder}` style tokens (Pass 1 F19 had flagged one such case in `memory.py`). A future handler addition could ship an undiscoverable command or a help entry pointing to a non-existent handler. | Added `tests/test_i18n_parity.py` with two test classes: `TestStringSubstitution` (parametrised over every `STRINGS` key — verifies `%s`/`%d` substitution produces a clean output with no leftover format specs, and no `{name}` style placeholder survives) and `TestHelpParity` (`handler.keys()` ⟷ `help_text` set-equality across registered commands). +148 parametrised tests. | **fixed** |
| P2-70 | ai_invoke.py:431 | Security | Med | The AI CLI subprocess was spawned without an explicit `env=` argument, so it inherited the full parent environment — including `ROBYX_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. A crash dump from the CLI that echoed env, or a prompt-injected agent that read `os.environ`, would surface bot secrets the CLI never needed. Trust-boundary X-1. | Added `_scrubbed_child_env()` helper that returns a copy of `os.environ` with `_SCRUBBED_ENV_KEYS` removed (the five bot-platform tokens + the legacy `KAELOPS_BOT_TOKEN` alias). Used as `env=` on `create_subprocess_exec`. Denylist (not allowlist) so user-set env like `HTTP_PROXY`, `LANG`, and provider API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) continue to pass through. +7 regression tests. | **fixed** |
| T065 | handlers.py | Security | — | Security audit against `trust-boundaries.md` (auth-before-mutation, AI-controlled path validation, command-injection residuals, secret-in-error-reply) | **Audit clean**: `owner_only` applied to every `cmd_*` and gated before the collab bypass in `handle_message`; Pass 1 `WORKSPACE` allow-list (S1) + SEND_IMAGE allow-list (S2) still in place; no shell injection primitives; `str(e)` echoed to users is all controlled `ValueError` from `create_workspace`/`create_specialist`. One marginal case at line 485 for generic `_process_and_send` exceptions flagged as low-priority hardening — not a verified gap given the AI CLI tools we support don't echo secrets. | noted |

