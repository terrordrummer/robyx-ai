# Implementation Plan: Full Code Review & Hardening

**Branch**: `002-full-code-review` | **Date**: 2026-04-16 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/002-full-code-review/spec.md`

## Summary

Systematic code review of all 29 Python modules under `bot/` (~12,300 lines)
to identify and fix: bugs, error handling gaps, security issues, dead code,
inconsistent patterns, and performance bottlenecks. Every finding is fixed
in-place with a corresponding test. Module-by-module approach for bisectable
changes.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: python-telegram-bot, discord.py, slack-sdk, python-dotenv, PyYAML, Pillow
**Storage**: JSON files under `data/`, SQLite for memory (new in 0.21.0)
**Testing**: pytest (1089+ tests in `tests/`)
**Target Platform**: macOS, Linux, Windows
**Project Type**: Python application (messaging bot / agent orchestrator)
**Scale/Scope**: 29 core modules, 12,298 LOC, 1089 tests

## Constitution Check

*GATE: Must pass before review begins.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Multi-Platform Parity | PASS | Review will check all 3 platform adapters equally |
| II. Chat-First Configuration | PASS | No new config introduced |
| III. Resilience & State Persistence | PASS | Review specifically targets error handling and crash safety |
| IV. Comprehensive Testing | PASS | Every fix gets a test |
| V. Safe Evolution | PASS | No data structure changes — pure code quality |

## Review Methodology

### Categories of Findings

| Category | Severity | Description |
|----------|----------|-------------|
| BUG | High | Logic errors, race conditions, unhandled exceptions |
| SEC | High | Injection, path traversal, token leakage, insecure defaults |
| ERR | Medium | Missing error handling, bare excepts, swallowed errors |
| PERF | Medium | Blocking I/O in async, redundant file reads, O(n^2) patterns |
| DEAD | Low | Unused imports, unreachable code, obsolete functions |
| STYLE | Low | Inconsistent naming, logging format, pattern mismatches |

### Review Checklist (per module)

For each module, check:
1. Error paths: does every I/O and external call have appropriate error handling?
2. Security: are inputs validated? Are tokens/credentials protected from leakage?
3. Async correctness: no blocking calls in async functions? No fire-and-forget tasks?
4. Resource management: files/connections closed? Context managers used?
5. Dead code: unused imports, unreachable branches, obsolete functions?
6. Consistency: naming, logging, patterns match the rest of the codebase?
7. Edge cases: empty inputs, None values, concurrent access?

## Module Groups (review order by priority)

### Group A: Core / High Risk (5 modules, ~4,650 LOC)

These handle message routing, AI execution, and scheduling — the critical path.

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/handlers.py` | 1562 | Input parsing, command dispatch, auth checks |
| `bot/scheduler.py` | 1404 | Timer precision, concurrent task execution, crash recovery |
| `bot/ai_invoke.py` | 832 | Subprocess management, output parsing, timeout handling |
| `bot/bot.py` | 854 | Startup/shutdown, service lifecycle, signal handling |
| `bot/updater.py` | 866 | Auto-update, snapshot/rollback, file operations |

### Group B: Platform Adapters (4 modules, ~1,010 LOC)

Cross-platform parity — each adapter must handle the same edge cases.

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/messaging/telegram.py` | 284 | Rate limiting, message splitting, error recovery |
| `bot/messaging/discord.py` | 279 | Thread management, permissions, reconnection |
| `bot/messaging/slack.py` | 242 | Socket mode, token refresh, event dedup |
| `bot/messaging/base.py` | 203 | ABC completeness, contract consistency |

### Group C: Agent & Task Management (6 modules, ~1,530 LOC)

Agent lifecycle, task execution, memory, and workspace management.

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/agents.py` | 409 | Session state, concurrent access, stale state |
| `bot/continuous.py` | 268 | State file I/O, interruption handling |
| `bot/task_runtime.py` | 142 | Context resolution, missing agent handling |
| `bot/scheduled_delivery.py` | 161 | Output routing, silent delivery, error propagation |
| `bot/topics.py` | 584 | Topic creation, naming, platform-specific behavior |
| `bot/collaborative.py` | 362 | Auth, routing, workspace sharing |

### Group D: Configuration & Support (8 modules, ~1,150 LOC)

Config, backends, memory, media, voice, i18n.

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/ai_backend.py` | 499 | Backend detection, model resolution, path handling |
| `bot/config.py` | 239 | Env var parsing, type coercion, missing values |
| `bot/config_updates.py` | 108 | .env file mutation, race conditions |
| `bot/memory.py` | 310 | SQLite fallback, legacy compat |
| `bot/memory_store.py` | 364 | Connection management, SQL injection |
| `bot/model_preferences.py` | 91 | YAML parsing, alias resolution |
| `bot/media.py` | 116 | Image compression, file handling |
| `bot/voice.py` | 41 | API calls, error handling |

### Group E: Infrastructure (6 modules, ~800 LOC)

Bootstrap, process management, auth, i18n, sessions, orphan tracking.

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/_bootstrap.py` | 194 | Import safety, dependency detection |
| `bot/process.py` | 114 | Subprocess lifecycle, signal propagation |
| `bot/authorization.py` | 81 | Permission checks, bypass risks |
| `bot/i18n.py` | 143 | String formatting, missing keys |
| `bot/session_lifecycle.py` | 162 | Invalidation logic, timing |
| `bot/orphan_tracker.py` | 137 | Orphan detection, cleanup |

### Group F: Migration Framework (5 core + 18 version files)

Review core framework only. Version files are trivial (most are no-ops).

| Module | LOC | Risk Areas |
|--------|-----|------------|
| `bot/migrations/runner.py` | 142 | Chain ordering, error recovery |
| `bot/migrations/tracker.py` | 98 | JSON persistence, corruption |
| `bot/migrations/legacy.py` | 394 | Legacy compat, edge cases |
| `bot/migrations/base.py` | 63 | Type definitions |
| `bot/migrations/__init__.py` | 100 | Module discovery |

## Project Structure

### Documentation (this feature)

```text
specs/002-full-code-review/
├── plan.md              # This file
├── spec.md              # Feature specification
├── tasks.md             # Task list (created by /speckit.tasks)
└── checklists/
    └── requirements.md  # Quality checklist
```

### Source Code

No new files. All changes are in-place modifications to existing modules
under `bot/` and new/updated tests under `tests/`.

## Complexity Tracking

> No Constitution Check violations. No new complexity introduced.
> This feature removes complexity (dead code) rather than adding it.
