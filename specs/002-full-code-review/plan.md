# Implementation Plan: Full Code Review & Hardening — Pass 2

**Branch**: `002-full-code-review` | **Date**: 2026-04-16 | **Spec**: [spec.md](./spec.md)
**Pass**: 2 of N (pass 1 completed in commits `ea3db78`, `3d0289f`, `e765259`; findings in [findings.md](./findings.md))

## Summary

Second code-review pass over the Robyx codebase. Pass 1 focused on developer-facing
quality (bugs, dead code, coarse security, performance). Pass 2 rotates the lens
90° toward **the running system and the end user**:

1. **Security** — threat-model each external surface (bot tokens, uploaded media,
   AI-controlled paths, migration inputs, webhook/socket payloads) and look for
   what a lens-1 review misses: trust boundaries, TOCTOU races, partial-failure
   auth bypasses, log-and-leak, DoS knobs.
2. **Stability** — crash-survival matrix: what happens at SIGKILL mid-write, mid-
   migration, mid-AI-invocation, with a corrupted JSON, with a full disk, with
   clock skew, with a stalled AI subprocess. Every unrecoverable state found must
   become recoverable.
3. **Facilità di utilizzo (ease of use)** — the user should never have to leave
   chat, never see a stack trace, never guess what command to run. Review every
   user-visible string, error path, command ergonomics, onboarding flow, and
   discoverability signal.
4. **Interazione naturale (natural interaction)** — the bot must sound like a
   collaborator, not a CLI. Review prompts, canned responses, error phrasing,
   single-locale tone consistency (the codebase is English-only today — future
   locales would inherit the same contract), silence policy (when NOT to talk),
   and turn-taking. Flag template leakage, over-apologizing, robot-speak, and
   inconsistency with agent persona.

Pass 1's findings report stays intact as historical record. Pass 2 produces a
**new findings table** appended to `findings.md` under `## Pass 2 Findings`, plus
a revised tasks list.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: python-telegram-bot, discord.py, slack-sdk,
python-dotenv, PyYAML, Pillow, sqlite3 (stdlib), optional sqlite-vec
**Storage**: JSON under `data/` + SQLite (`data/memory.db`) since v0.21.0
**Testing**: pytest (1086 tests, all passing at plan time)
**Target Platform**: Linux/macOS/Windows service (launchd / systemd / Task Scheduler)
**Project Type**: Single-project Python service (no frontend, no external DB)
**Performance Goals**: 60 s scheduler tick budget; interactive chat reply ≤ 2 s
from handler entry to platform `send_message` for non-AI commands
**Constraints**: single-instance (PID lock), unattended operation, Chat-First
config (no file-editing required), Multi-Platform parity across Telegram /
Discord / Slack
**Scale/Scope**: 53 modules under `bot/`, 12 329 LOC, 18 migrations
(`v0_20_12` → `v0_21_0`), 1086 tests (1085 passed + 1 skipped)

No unresolved NEEDS CLARIFICATION — the codebase itself is the source of truth.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Pass 2 Compliance |
|-----------|-------------------|
| I. Multi-Platform Parity | Any UX/interaction fix MUST ship across Telegram/Discord/Slack adapters or be explicitly marked as a parity gap in findings. |
| II. Chat-First Configuration | Usability findings MUST be fixable via chat; if a finding would force the user to edit files, the fix must also expose a chat command. |
| III. Resilience & State Persistence | Stability is the central lens. Every stability finding MUST either be fixed or justified as out-of-scope with rationale. |
| IV. Comprehensive Testing | Every bug/security/stability fix ships with a regression test (continues FR-004 from spec). UX/phrasing fixes need at least one assertion-based test on the string or i18n key. |
| V. Safe Evolution | No schema changes expected; if one is needed, it goes through `bot/migrations/vX_Y_Z.py`. Phrasing changes to user-visible strings are non-breaking by construction. |

**Initial gate**: PASS — no violations, no entries needed in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/002-full-code-review/
├── spec.md              # Original spec (Pass 1 scope)
├── plan.md              # This file — Pass 2 plan
├── findings.md          # Pass 1 findings (kept) + Pass 2 findings (appended)
├── tasks.md             # Pass 1 tasks (kept) + Pass 2 tasks (appended by /speckit.tasks)
├── research.md          # Phase 0 output (Pass 2)
├── quickstart.md        # Phase 1 output — how to reproduce this review locally
├── contracts/
│   └── conversation-contract.md   # Phase 1 — user-visible-string & tone contract
└── checklists/          # (from prior work, kept)
```

### Source Code (repository root)

```text
bot/
├── _bootstrap.py
├── agents.py
├── ai_backend.py
├── ai_invoke.py
├── authorization.py
├── bot.py
├── collaborative.py
├── config.py
├── config_updates.py
├── continuous.py
├── handlers.py
├── i18n.py                      # <- major Pass 2 focus (natural interaction)
├── media.py
├── memory.py
├── memory_store.py
├── messaging/
│   ├── base.py
│   ├── telegram.py
│   ├── discord.py
│   └── slack.py                 # <- parity audit focus
├── migrations/
│   ├── runner.py
│   ├── tracker.py
│   └── v0_20_12.py … v0_21_0.py (25 files)
├── model_preferences.py
├── orphan_tracker.py
├── process.py
├── scheduled_delivery.py
├── scheduler.py                 # <- stability focus
├── session_lifecycle.py
├── task_runtime.py
├── topics.py
├── updater.py                   # <- security focus (tarball + pip)
└── voice.py

tests/
├── conftest.py
└── test_*.py  (32 files, 1086 tests)
```

**Structure Decision**: Single-project layout unchanged. Pass 2 does not add
modules; it rewrites in place. Review groups are reused from Pass 1 (A–F) but
each group is re-traversed with the new four-lens checklist rather than the
old four-user-story checklist.

## Review Groups (re-used from Pass 1)

| Group | Modules | Pass 2 lens priority |
|-------|---------|----------------------|
| A — Core / high-risk | `handlers.py`, `scheduler.py`, `ai_invoke.py`, `bot.py`, `updater.py` | Security + Stability |
| B — Platform adapters | `messaging/{telegram,discord,slack,base}.py` | Stability + Natural Interaction + Parity |
| C — Agents & tasks | `agents.py`, `continuous.py`, `task_runtime.py`, `scheduled_delivery.py`, `topics.py`, `collaborative.py` | Stability + Ease of use |
| D — Config & support | `ai_backend.py`, `config.py`, `config_updates.py`, `memory.py`, `memory_store.py`, `model_preferences.py`, `media.py`, `voice.py` | Security + Ease of use |
| E — Infrastructure | `_bootstrap.py`, `process.py`, `authorization.py`, `i18n.py`, `session_lifecycle.py`, `orphan_tracker.py` | Security + Natural Interaction (`i18n`) |
| F — Migration framework | `migrations/runner.py`, `tracker.py`, `base.py`, `legacy.py`, migration files | Stability |

## Four-Lens Checklists (applied per module)

### Lens 1 — Security (deep)
- Trust boundary for every input path (platform payload → handler → AI subprocess → filesystem).
- TOCTOU: any `stat → open` / `exists → read` / `isdir → write` pair reviewed for races.
- Secret handling: tokens never in logs, never in tracebacks, never in error replies to the user.
- Path handling: every filesystem write validated against `WORKSPACE` / `DATA_DIR` / allow-list.
- Subprocess: argv arrays only (no `shell=True`), env scrubbed of host secrets unneeded by AI CLI.
- Deserialization: JSON size/depth limits, YAML `safe_load`, tarball member validation (already added in Pass 1 — re-verify).
- DoS knobs: any attacker-controlled loop bound, unbounded retry, unbounded allocation.
- Authorization: every handler checks `OWNER_ID`, collaborative auth check on shared workspaces.

### Lens 2 — Stability
- SIGKILL-survive: mid-write files go through `tmp + rename`.
- Migration idempotency: apply-twice-in-a-row produces identical state.
- Scheduler late-fire: missed ticks replay without duplicates.
- AI subprocess: orphan tracker sees it even on parent crash; timeouts enforced.
- Disk full: no unhandled `OSError: [Errno 28]`.
- Corrupted JSON: load-time recovers with backup + warning, never crashes.
- Clock skew / monotonic time: timers don't fire wrong-direction on NTP jump.
- Restart storm: keep-alive + single-instance lock survive 10 crashes/min.

### Lens 3 — Ease of use
- Every user-visible error is actionable (says what to do, not only what failed).
- Stack traces never reach the user — only internal logs.
- Commands are discoverable via `/help`; `/help` is accurate and current.
- Onboarding: first-time user message flow works end-to-end across all 3 platforms.
- Required env vars missing → clear message, not a crash; `.env` template up to date.
- Destructive commands (`/reset`, `/remove`, `/clear_memory`) require confirmation.

### Lens 4 — Natural interaction
- All user-visible strings live in `i18n.py`; no hard-coded strings in handlers.
- IT/EN parity: for every key, both locales exist and convey the same intent.
- Tone audit: no `Error:` / `Exception:` / `Fatal:` prefixes leaking to users; no
  "The system has …" impersonal phrasing where the bot could speak in first person.
- Template leakage: `{placeholders}` never appear unsubstituted.
- Silence policy: bot does NOT reply to acknowledge internal events the user did
  not trigger (scheduled task progress = silent by default since v0.20.27 —
  re-verify).
- Turn-taking: no "typing …" race that sends a partial reply then overwrites it.
- Agent persona: prompts in `templates/` don't contradict agent-specified persona.

## Phase 0: Research

See [research.md](./research.md). Topics:

1. **Threat model per adapter** — what a hostile Telegram/Discord/Slack user
   can send, what adapter accepts, what the rest of the system trusts.
2. **Crash-survival matrix** — concrete scenarios × current behavior × desired
   behavior.
3. **Natural-interaction heuristics** — checklist adapted from conversational
   UX research (discoverability, error tone, progressive disclosure).
4. **String inventory method** — `grep` pattern to find hard-coded user-facing
   strings outside `i18n.py`.
5. **Baseline metrics** — test count, LOC, known-gap count from Pass 1
   ("Deferred Findings" in findings.md).

**Output**: research.md with per-topic decisions and rationales.

## Phase 1: Design & Contracts

**Prerequisites**: research.md complete.

### Data model

**N/A** — code review introduces no new entities. Existing `data/` schema is
unchanged. If a fix requires a schema change (e.g. adding a retry counter to a
queue record), it triggers a new migration file, not a change to `data-model.md`.

### Contracts

Exposed contracts to document for Pass 2:

1. **`contracts/conversation-contract.md`** — the implicit contract the bot has
   with the user: tone, silence policy, error phrasing rules, i18n coverage rule.
   This is the reference a reviewer uses to decide "is this reply acceptable?".

No HTTP/API contracts (Robyx is a consumer of platform APIs, not a provider).

### Quickstart

**`quickstart.md`** documents how a reviewer:
1. Checks out the branch, runs `pytest` to record baseline.
2. Walks a module with the four-lens checklist.
3. Files a finding row in `findings.md` under `## Pass 2 Findings`.
4. Fixes the finding with a regression test.
5. Re-runs `pytest` and verifies baseline is met or exceeded.

### Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` after this plan is
written so `CLAUDE.md` picks up the new technology/focus entries.

**Output**: quickstart.md, contracts/conversation-contract.md, updated CLAUDE.md.

## Post-Design Constitution Re-Check

Re-reading the five principles after Phase 1 artifacts are written:

- I. Parity — conversation contract explicitly requires it. ✅
- II. Chat-First — ease-of-use lens enforces it. ✅
- III. Resilience — stability lens is central. ✅
- IV. Testing — each fix still requires a test. ✅
- V. Safe Evolution — no expected migrations; if one is needed, runner path is intact. ✅

**Post-design gate**: PASS.

## Complexity Tracking

*No violations — table intentionally empty.*

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| — | — | — |
