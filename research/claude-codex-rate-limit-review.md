# Claude ↔ Codex — rate-limit false-positive review

Iterative adversarial review between Claude (implementor) and Codex (codex-cli
0.128.0, second-opinion peer architect). Setup: Claude drives via
`codex exec` / `codex exec resume --last`, captures via `-o`.

**Subject**: `bot/ai_invoke.py` rate-limit detection logic (lines 296-299,
574-588, 824, 891-892) and the `_read_stream` event handling (lines 943-1021).

**Reported symptom**: working inside the robyx-ai workspace, the user
frequently received chat replies replaced with
`"Rate limit reached — retry in a few minutes."` despite being far from
their Anthropic subscription's actual usage limits.

**Convergence**: reached in 3 turns. Sign-off Codex on 2026-05-14.

---

## Turn 1 — Claude → Codex (richiesta di review)

Prompt sintetico (full text in `/tmp/codex-review-rate-limit/prompt-1.txt`):
> Adversarial review of rate-limit detection. Hypothesis: substring keyword
> matching on assistant `text` content (line 891) and on `combined =
> stdout+stderr` (line 579) produces false positives whenever the agent
> discusses rate-limit code (very common in this codebase). 6 specific
> scrutiny points. Codex told to ask for clarification rather than invent.

## Turn 1 — Codex → Claude

6 verdicts:

1. **blocker** — Success path substring scan on assistant `text` confirmed.
   Cites `templates/CONTINUOUS_STEP.md:126` ("If you encounter a rate limit
   error"), `bot/i18n.py:51`, `bot/ai_invoke.py:296`. Action: remove the
   check; if structured detection wanted, `_read_stream` must return event
   metadata.

2. **major** — Partial correction: `bot/ai_invoke.py:824` is the
   non-streaming branch, ClaudeBackend uses streaming, so this path
   doesn't apply to Claude in practice. Still applies to Codex/OpenCode.
   `combined = stdout + stderr` includes assistant JSON content. Action:
   classify on stderr first; stdout only as fallback or via backend-
   specific parsing.

3. **major** — Real rate-limit detection should use Claude Code stream-json
   `system/api_retry` events + final `result.subtype`/`is_error`. Codex
   explicitly states it does NOT know the exact terminal format and asks
   Claude for clarification.

4. **major** — `"throttl"` substring (no boundary) matches `throttler`,
   `throttle_count`, `unthrottled`. Action: regex `\bthrottl(?:e|ed|ing)\b`
   or remove from free-form scans.

5. **major** — Other false positives ("limit reached" → max-turns/budget,
   "over capacity" → 503 overload, "quota exceeded" → billing) and false
   negatives (`rate_limit_error`, `429`, `RateLimitError`). Action: split
   into categories.

6. **minor** — `_is_rate_limited` is case-sensitive (defensive only since
   call sites lowercase first). Also: `_read_stream` sets
   `result_text = event.get("result", "") or ""` for a result event with
   no `result` field, returning `""`; downstream becomes
   `STRINGS["ai_empty"]` losing potential subtype/is_error.

**Convergenza?** Not frozen — blocker on item 1, plus needs sample of real
Claude Code rate-limit output before structured detection can be implemented.

(Full text in `/tmp/codex-review-rate-limit/response-1.md`.)

---

## Turn 2 — Claude → Codex (fix applicati + giustificazione differimenti)

Prompt sintetico (full text in `/tmp/codex-review-rate-limit/prompt-2.txt`):

**Applied**:
- Item 1 (blocker): removed `_is_rate_limited(text_lower)` on success
  path (line 891-892). Replaced with explanatory comment.
- Item 2 (major): `_classify_error` now uses
  `haystack = err.lower() if err else combined`, preferring stderr.
  Stdout-only keyword matches are now ignored.
- Item 6 minor: `_is_rate_limited` defensively case-insensitive
  (internal `.lower()`).

**Tests**: inverted `test_rate_limited_response` (was enforcing the bug)
to `test_assistant_text_mentioning_rate_limit_is_delivered`; added
`test_stderr_preferred_over_stdout_for_classification` and
`test_falls_back_to_combined_when_stderr_empty`. 142/142 pass.

**Deferred with explicit reasoning**:
- Item 3 (structured detection): Codex itself flagged uncertainty; no
  sample of real rate-limit output available.
- Item 4 ("throttl"): false-positive risk dropped to near-zero now that
  the check runs on stderr only; CLI stderr unlikely to contain
  `throttler`/`throttle_count`.
- Item 5 false negatives: same reasoning as item 3.
- Item 6 result_event: changes `_read_stream` return contract; blast
  radius beyond the reported symptom.

Plus raised a new related concern (turn-2 item 3): `STREAM_RETRYABLE_KEYWORDS`
shares the same anti-pattern at line 875 — assistant discussing networking
triggers silent retries. In-scope for this review or follow-up?

## Turn 2 — Codex → Claude

1. **ok, with one minor** — Comment "A true rate-limit surfaces as
   non-zero exit + stderr keyword" asserts un-verified behavior. Codex
   proposes: "Do not infer rate limits from assistant content; failure-
   path diagnostics are classified separately."

2. **ok** — Fix is sufficient. Minor suggestion:
   `haystack = (err if err else combined).lower()` for defensive
   re-lowercase even if a future caller passes non-lowercase combined.

3. **major separato, non blocker** — `STREAM_RETRYABLE_KEYWORDS` same
   pattern. Should be follow-up; not blocker for the user's reported
   symptom. Same fix shape (stderr-preferred on failure path, no
   substring scan on assistant content on success path).

**Convergenza?** Frozen after the two minor corrections. No blocker
residuo on 1/2. `STREAM_RETRYABLE_KEYWORDS` as separate major follow-up.

---

## Turn 3 — Claude → Codex (minor applicati, richiesta sign-off finale)

Applied both minor corrections:
- Comment rewritten as Codex proposed.
- `haystack = (err if err else combined).lower()` applied.

142/142 tests still pass.

## Turn 3 — Codex → Claude

**Verdetto**: ok. Verified the actual git diff (`git diff -- bot/ai_invoke.py
tests/test_ai_invoke.py`), confirmed both minor are present.

**Convergenza?** Frozen for the rate-limit false-positive fix. Follow-up
punch list confirmed, especially `STREAM_RETRYABLE_KEYWORDS` as separate
major.

— *Review chiusa. Sign-off Codex T3 il 2026-05-14. 3 turni, 142/142 tests
pass, 1861/1861 full suite pass.*

---

## Final disposition

| Codex item | Status | Reason |
|---|---|---|
| T1.1 success-path substring scan (blocker) | **applied** | Direct cause of false positive |
| T1.2 `_classify_error` haystack (major) | **applied** | Same root cause for non-Claude backends |
| T1.3 structured stream-json detection (major) | **deferred** | Codex itself asked for clarification on event format |
| T1.4 "throttl" substring (major) | **deferred (indirectly resolved)** | After stderr-only restriction, FP risk negligible |
| T1.5 keyword categorization + false negatives (major) | **deferred** | Requires sample + policy decision |
| T1.6 case-sensitivity defensive (minor) | **applied** | Cheap, no downside |
| T1.6 `_read_stream` is_error/subtype (minor) | **deferred** | Changes return contract; blast radius |
| T2.1 comment phrasing (minor) | **applied** | Removed un-verified assertion |
| T2.2 lower()-the-fallback (minor) | **applied** | Defensive |
| T2.3 `STREAM_RETRYABLE_KEYWORDS` (major separate) | **filed as follow-up** | Different symptom; both agents agreed scope-separate |

## Follow-up backlog

Major items both agents agreed are real but out of scope for this fix:

1. **`STREAM_RETRYABLE_KEYWORDS` substring scan on assistant text** — same
   anti-pattern as the rate-limit check just removed. Lines 806, 846, 875.
   Symptom: assistant discussing networking ("connection reset by peer",
   "broken pipe") triggers silent retry up to `MAX_AI_RETRIES` with fresh
   session, causing duplicated runs and side-effects. Fix shape: only
   retry on error-shaped payloads (e.g., `"API Error:"` prefix) on the
   success path; stderr-preferred on the non-streaming failure path.

2. **Structured rate-limit detection via Claude Code stream-json events
   (`is_error`, `subtype`, `system/api_retry`)** — requires capturing a
   real-world rate-limit run to verify the exact event format before
   implementation. Will reduce false negatives (currently we won't
   detect a real rate-limit unless its keyword appears in stderr).

3. **Keyword categorization** — distinguish rate-limit (retry-soon),
   quota/billing (longer-term), overloaded (transient server), network
   (transient client). Different recovery semantics.

4. **`_read_stream` event metadata** — return enough info to surface real
   errors (subtype, is_error) rather than collapsing them into
   `STRINGS["ai_empty"]`. Affects return contract; coordinate with the
   structured-detection effort above.
