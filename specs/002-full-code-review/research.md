# Phase 0 Research: Code Review Pass 2

**Date**: 2026-04-16
**Status**: Complete (no open NEEDS CLARIFICATION)

---

## R1 — Threat model per platform adapter

**Decision**: Treat every adapter as an untrusted boundary. The adapter is the
only place a hostile payload enters the process; downstream code may assume
types and lengths are already validated.

### Telegram (`bot/messaging/telegram.py`)

| Input | Attacker-controllable? | Current validation | Gap to close |
|-------|------------------------|--------------------|--------------|
| `update.message.text` | Yes, any authorised user | truncation at platform layer | Check handler assumptions on max length before passing to AI |
| `update.message.voice.file_id` | Yes | downloaded to temp file | Size cap before download, extension whitelist |
| `update.message.document.file_name` | Yes | None | Path-traversal in any place that concatenates it into a filesystem path |
| `update.message.photo[*].file_id` | Yes | Pillow reads bytes | Max dimension check (already partly in `media.py` — re-verify against decompression bombs) |
| Chat/thread IDs | Yes (spoofable between groups the bot is in) | `OWNER_ID` check | Verify check happens BEFORE state mutation, not after |

### Discord (`bot/messaging/discord.py`)

| Input | Attacker-controllable? | Current validation | Gap to close |
|-------|------------------------|--------------------|--------------|
| `message.content` | Yes | None | Same as Telegram |
| `message.attachments[*].url` | Yes | Pass 1 added domain allow-list for voice | Extend allow-list check to ALL attachment fetches, not only voice |
| Thread/channel IDs | Yes | Authorization check | Race: thread created mid-handler may skip auth |

### Slack (`bot/messaging/slack.py`)

| Input | Attacker-controllable? | Current validation | Gap to close |
|-------|------------------------|--------------------|--------------|
| `event.text` | Yes | None | Same as Telegram |
| `event.files[*].url_private` | Yes, but requires auth token to download | Token added in download | Ensure token never echoed back in error replies |
| Socket Mode envelope `event_id` | Yes | Used for dedup | Dedup store bounded? (check memory growth) |

**Rationale**: A single checklist applied uniformly to three adapters is easier
to audit than ad-hoc per-platform rules. Parity in *threat model* reinforces
parity in *code*.

**Alternatives considered**:
- Per-platform threat docs — rejected: duplication drifts.
- Moving all validation to a middleware layer — rejected: adapters already
  implement the shared ABC; a middleware would be a fourth place to look.

---

## R2 — Crash-survival matrix

**Decision**: Catalog failure modes as a matrix; each row is a scenario, each
cell is current-vs-desired behavior. Rows where current ≠ desired become
Pass 2 findings.

| # | Scenario | Current behavior (best guess) | Desired behavior |
|---|----------|-------------------------------|------------------|
| C1 | SIGKILL mid-write `data/queue.json` | File may be partial — JSON load fails on restart | tmp+rename pattern → partial file is old file |
| C2 | SIGKILL mid-migration | `migrations/tracker.py` may have advanced version but data half-applied | Migrations are transactional per version; advance version only after write fsync |
| C3 | AI subprocess stalls past timeout | `ai_invoke.py` kills it; orphan tracker captures PID | Verify: PID file removed even if parent crashes between kill and cleanup |
| C4 | Disk full on `data/` write | Unhandled `OSError: [Errno 28]` | Surface to user with actionable message; do not corrupt existing file |
| C5 | Corrupted `data/agents.json` at startup | JSON decode error → crash loop | Load-time validator with backup-and-rebuild path |
| C6 | Clock NTP jump backward | Scheduler may fire again for past events | Use `time.monotonic` for intervals; wall clock only for deadlines |
| C7 | Restart storm (10 crashes/min) | PID lock + systemd restart; state survives if C1 fixed | Confirmed via test harness |
| C8 | Two bot processes racing on `bot.pid` | Second process exits on lock | Verify lock is POSIX `fcntl.flock` or equivalent (not PID-file-check race) |
| C9 | AI CLI binary missing | `ai_backend.py` raises at import or invoke | Clear user-facing message (via `i18n`), not a traceback |
| C10 | `data/memory.db` locked by another process | SQLite `OperationalError` | Retry with backoff; escalate to user if persistent |
| C11 | Continuous task partial step | `continuous.py` writes progress per step | Verify step boundary is atomic; re-run from last committed step |
| C12 | .env hot-edited while running | `config_updates.py` watches file | No reload mid-AI-call (race on secrets) |

**Rationale**: Pass 1 fixed 15 bug findings but was driven by code inspection,
not by adversarial scenario design. Pass 2 starts from the scenario and maps
back to code.

**Alternatives considered**:
- Chaos-testing framework — rejected for Pass 2 scope: too much infra work
  for marginal gain vs. a scenario checklist.

---

## R3 — Natural-interaction heuristics

**Decision**: Adapt four well-known conversational-UX principles into reviewable
checks.

1. **Actionability** — every error reply must contain: (a) what happened, (b)
   what the user can do about it. Example bad: `Error: invalid state.` Example
   good: `Can't create an agent named "default" — that name is reserved. Try a
   different name.`
2. **Tone consistency** — bot always speaks in first person plural ("proviamo
   …") or second person ("hai detto …"), never impersonal ("il sistema ha
   rilevato …"). Exception: AI-produced output retains the AI's voice.
3. **Silence policy** — the bot does not emit unsolicited messages for internal
   events (scheduler tick, cache refresh, memory write). Exceptions:
   user-requested notifications, errors on user-initiated actions, proactive
   reminders the user explicitly scheduled.
4. **Progressive disclosure** — `/help` shows a short default summary; details
   come through `/help <topic>` or contextual hints. A wall of commands on
   first use is a failure.

**Implementation as review checklist**: for each user-visible string touched
by Pass 2, reviewer must answer yes/no on all four questions. "No" → finding.

**Rationale**: Robyx's value prop is "Clone. Configure. Talk." — the "Talk"
verb implies a conversational standard, not just a working interface.

**Alternatives considered**:
- Full i18n rewrite into a conversational framework — rejected: out of scope,
  too invasive.
- Using an LLM as tone judge — rejected: non-deterministic, hard to verify
  in tests.

---

## R4 — String-inventory method

**Decision**: Two-pass sweep to find user-visible strings outside `i18n.py`.

1. `rg -n '"[A-Z][a-z].*"' bot/ --type py -g '!i18n.py'` — any string literal
   starting with a capital letter in source is a candidate (English prose).
2. `rg -n 'send_message\(|reply\(|edit_message\(' bot/ --type py -B2` — any
   message-sending call should have its string argument traced back to an
   `i18n` key.

A finding is filed for each literal that: (a) is shown to the user, and (b)
is not sourced via `i18n.t(...)`.

**Rationale**: We can't review what we can't find. The pattern above is noisy
but exhaustive; false positives are quick to dismiss.

**Alternatives considered**:
- AST-based extractor — rejected: overkill for a one-shot audit.
- Relying on code review by eye — rejected: Pass 1 proved this misses
  adapter-local strings (error messages in exception paths).

---

## R5 — Baseline metrics

**Decision**: Lock in baseline before Pass 2 begins; measure delta at end.

| Metric | Value (2026-04-16) | Source |
|--------|-------------------|--------|
| Test count (collected) | 1086 | `pytest --co -q` |
| Python LOC under `bot/` | 12 329 | `find bot -name '*.py' \| xargs wc -l` |
| Modules under `bot/` (`.py` files) | 53 | `find bot -name '*.py' \| wc -l` |
| Migrations | 25 (`v0_20_12` … `v0_21_0`) | `ls bot/migrations/v*.py` |
| Pass 1 deferred findings | 10 (F12 F13 F14 F17 F20 + 5× performance P1-P5) | `specs/002-full-code-review/findings.md` |
| Version | 0.21.0 | `VERSION` |

**Acceptance gate for Pass 2 close**:
- Test count ≥ 1086 (new tests for every new finding fix).
- No regressions in existing tests.
- `## Pass 2 Findings` table filled, every row in state "fixed" or explicitly
  "deferred with rationale".
- Deferred Pass 1 findings re-evaluated — each one either fixed in Pass 2 or
  an explicit "still deferred" note with updated rationale.

**Rationale**: Pass 1 had a measurable close criterion; Pass 2 should too.

**Alternatives considered**:
- LOC-reduction target as in Pass 1 (5%) — rejected: Pass 2 is about
  correctness/UX, not cleanup, and extra code (better error messages) is
  expected.
