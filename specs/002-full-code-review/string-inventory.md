# String Inventory — Pass 2 T062

**Date**: 2026-04-16
**Method**: ripgrep sweeps from `research.md` §R4, narrowed to strings that are
directly passed to `send_message` / `reply` / `edit_message` and equivalents
(`reply_text`, `send_text`, `edit_text`). Strings raised inside exceptions
that may bubble to a user are captured separately.

**Scope**: `bot/**/*.py`. Excluded: `bot/__pycache__/`, `tests/`.

**Baseline `t()` adoption**: 241 `t("key"…)` calls across 25 files — i18n
discipline is already widespread. This inventory catalogues the gaps.

---

## §A — Hard-coded literals passed to messaging calls (must route through `t()`)

| File | Line | Call site | Literal | Suggested i18n key |
|------|------|-----------|---------|--------------------|
| `bot/handlers.py` | 213 | `await platform.reply(msg_ref, "Usage: /reset <name>")` | `"Usage: /reset <name>"` | `reset_usage` (new) |
| `bot/handlers.py` | 306 | `sent_ref = await platform.reply(msg_ref, "Checking for pending update...")` | `"Checking for pending update..."` | reuse existing `update_checking` (same phrase minus ellipsis variance) or add `update_checking_manual` |
| `bot/handlers.py` | 1350 | `await platform.reply(msg_ref, "No users registered in this workspace.")` | `"No users registered in this workspace."` | `collab_no_users` (new; fits existing `collab_*` group) |

**Observation**: only three direct-literal violations across 105 messaging
call-sites. The codebase passes the ownership rule (contract §1) almost
universally.

---

## §B — Exception messages that may reach the user

These `raise` calls embed English prose in the exception message. If any of
them is caught by the handler layer and formatted into a reply (verified
per-callsite during Phase 9/11), the text should be sourced from `i18n`.

| File | Line | Exception | Message | Reviewer note |
|------|------|-----------|---------|---------------|
| `bot/messaging/discord.py` | 163 | `ValueError` | `"Refusing to download from non-Discord URL: %s"` | Security error — path is internal; acceptable as log text. Confirm handler does NOT echo `str(e)` to user; if it does, route through `t("voice_url_rejected")` |
| `bot/bot.py` | 290 | `ValueError` | `"Unsupported platform: %s"` | Startup error — user sees stderr/console on first launch. Acceptable as-is (error precedes bot-to-user channel). |
| `bot/ai_invoke.py` | 126 | `ValueError` | `"REMIND requires exactly one of at= or in="` | Programmer error (should be caught in validation), not user-facing. Acceptable. |
| `bot/media.py` | 53 | `MediaError("File not found: %s")` | same | Internal — media path is not user-visible except in error reply. Verify caller wraps in i18n. |
| `bot/media.py` | 55 | `MediaError("Not a regular file: %s")` | same | Same as above. |
| `bot/media.py` | 74 | `MediaError("Cannot open image %s: %s")` | same | Same as above. |

**Follow-up tasks** (Phase 11 / 12):
- Trace each `MediaError` catch-site and confirm users see an `i18n` key, not
  `str(e)`.
- If any catch-site passes `str(e)` directly to `reply`, that becomes a P2-NI
  finding.

---

## §C — String-building patterns that may hide user-visible literals

A cursory `grep` for `msg = "..."` / `text = "..."` followed by `reply(msg)` /
`send_message(text)` within the same function is not feasible without AST
analysis. For Phase 11/12 module-level audits, reviewers must scan each file
for this pattern manually.

**High-probability modules** (by messaging-call density):
- `bot/handlers.py` — 70 `send_message`/`reply` calls
- `bot/bot.py` — 15 calls
- `bot/ai_invoke.py` — 6 calls
- Each adapter (`telegram.py`, `discord.py`, `slack.py`) — 3-4 calls but
  adapter-local error paths

**Method for Phase 11/12**: for every `send_message`/`reply`/`edit_message`
line in the above files, trace back ≤ 20 lines to find the string source;
flag any literal not routed through `t()`.

---

## §D — Template files (`templates/*.md`)

Nine files:

- `CONTINUOUS_SETUP.md`
- `CONTINUOUS_STEP.md`
- `SCHEDULER_AGENT.md`
- `agent-template.md`
- `prompt_collaborative_agent.md`
- `prompt_focused_agent.md`
- `prompt_orchestrator.md`
- `prompt_workspace_agent.md`
- `specialist-template.md`

These contain prompts fed to the AI, not direct user-visible strings, but
they shape what the AI produces → the AI's output IS user-visible. Phase 12
T107/T108 must:

1. Verify every `{placeholder}` has a guaranteed substitution at the call site.
2. Verify tone of prompt instructions is consistent with contract §3 (the
   bot's persona should align with the prompts' instructions to it).

Inventory of placeholders per template file is deferred to Phase 12 T107
(module-level pass is the right time to sweep each).

---

## §E — Summary & action items

| Category | Count | Status |
|----------|-------|--------|
| §A — direct-literal messaging violations | 3 | filed as P2-01, P2-02, P2-03 |
| §B — exception prose that *may* reach users | 6 | reviewer to trace catch-sites during Phase 11 (US-UX) and Phase 12 (US-NI) |
| §C — string-building patterns | unknown | method described, executed during per-module audits |
| §D — templates | 9 files, placeholder count deferred | T107/T108 |

**Single-locale reality**: the inventory confirms `bot/i18n.py` is a single
English `STRINGS` dict. Multi-locale support is NOT a Pass 2 deliverable;
single-locale discipline is. Pass 2 conversation contract §2 updated
accordingly (see plan.md and contract file).
