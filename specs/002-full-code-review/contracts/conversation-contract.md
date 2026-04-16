# Conversation Contract

**Scope**: Every user-visible string produced by Robyx — error messages, help
text, status updates, prompts embedded in `templates/`, i18n-keyed replies.

This document is the reference a reviewer uses during Pass 2 to decide whether
a given reply is acceptable. It is not law; it is the explicit articulation of
the "Clone. Configure. Talk." promise.

---

## 1. Ownership rule

All user-visible strings MUST live in `bot/i18n.py` (or, for AI prompts, under
`templates/`). Inline string literals in handlers, adapters, or services are
contract violations.

**Exception**: debug-level log strings (`logger.debug(...)`) are internal and
exempt — they are never shown to users.

**Review question**: does this string reach `send_message`, `reply`, or
`edit_message`? If yes, it must be keyed through `i18n.t(...)`.

## 2. Single-locale discipline (and future locale parity)

Today `bot/i18n.py` is a single English `STRINGS` dict. The locale-parity
principle therefore currently reduces to: **every user-visible string MUST
come from `STRINGS` via `t("key", ...)`**. No sibling-locale file exists yet.

If and when a second locale is added (structure: rename `STRINGS` → per-locale
dicts such as `STRINGS_EN`, `STRINGS_IT` and add a locale selector to `t()`),
the parity rule becomes: every key MUST exist in every locale with matching
intent, and a test MUST fail if a key is orphaned in one locale.

**Review question (today)**: is the string routed through `t("<key>")`?
**Review question (post-multi-locale)**: does the key exist in all locales?

## 3. Tone rules

### 3.1 Voice
- Default voice: the bot speaks in first person plural when proposing
  ("let's try …") and second person when confirming user intent
  ("you said …"). (Examples are English-today; principle is language-neutral.)
- Forbidden: impersonal "The system has …" — the bot is an agent, not a system.

### 3.2 Error phrasing
- No leading `Error:` / `Exception:` / `Fatal:` prefixes. Failures are stated
  as facts, not as diagnostic labels.
- Every error reply MUST contain an actionable next step.
  - Bad: `Invalid state.`
  - Good: `Non riesco a creare l'agente — il nome "default" è riservato.
    Prova un nome diverso.`
- Stack traces NEVER reach users. They belong in logs.

### 3.3 Confidence
- Bot acknowledges uncertainty explicitly ("non sono sicuro di aver capito, è
  …?") rather than guessing silently.
- No over-apologizing. One "mi dispiace" per failure, not per sentence.

## 4. Silence policy

The bot does NOT emit messages for:
- scheduler tick events (a tick firing is internal);
- memory writes, cache refreshes, migration steps;
- success of any action the user did not initiate in that turn
  (e.g. auto-save of session state);
- background AI-invocation start (`"chiedo al modello …"` is acceptable only
  when latency exceeds the 2-second threshold).

The bot DOES emit messages for:
- any action the user initiated in the current turn (with a reply);
- errors in user-initiated actions;
- reminders and scheduled messages the user explicitly requested;
- proactive escalations on critical failures (e.g. AI CLI binary missing —
  user needs to act).

Silent-by-default for scheduled delivery is already enforced since v0.20.27;
Pass 2 re-verifies compliance module-by-module.

## 5. Template hygiene

- `{placeholder}` tokens never appear unsubstituted in output. If substitution
  fails, the reviewer's finding is both (a) the missing substitution data and
  (b) the lack of a defensive fallback in the format call.
- Double-braced escapes (`{{literal}}`) must be intentional and commented.

## 6. Progressive disclosure

- `/help` (no argument) shows a short, scannable command summary (max 12
  lines).
- `/help <command>` shows detail for one command.
- New commands MUST be added to the `/help` index in the same PR that
  introduces them; an orphan command (works but not listed) is a finding.

## 7. Parity across platforms

A user-visible string that differs between Telegram, Discord, and Slack
adapters is a parity violation (Constitution Principle I). Differences are
allowed only when platform capabilities force them (e.g. Markdown vs.
Slack mrkdwn syntax) — and such differences MUST be produced by the shared
`PlatformMessage` formatter, not by divergent strings in each adapter.

## 8. Pass 2 review checklist

For each user-visible string touched during Pass 2, reviewer answers:

1. Is it sourced from `i18n.t(...)` or a `templates/` file? (Ownership rule)
2. Do IT and EN versions both exist with matching intent? (Locale parity)
3. Is the voice consistent with §3.1? (Tone)
4. If it's an error, does it state an actionable next step? (§3.2)
5. Does any code path emit this string without user-initiated action? (§4)
6. Do all `{placeholders}` have guaranteed substitutions? (§5)
7. If it's a new command, is it listed in `/help`? (§6)
8. Do all three adapters produce the same user experience? (§7)

Any "no" is a finding in `findings.md` under `## Pass 2 Findings`.
