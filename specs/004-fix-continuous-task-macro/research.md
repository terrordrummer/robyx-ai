# Phase 0 — Research: Fix Continuous Task Macro Leak

## R-01 — Where does the leak originate today?

**Decision**: The primary leak sites are (a) the `is_robyx and cont_match and
prog_match` conjunction in `bot/handlers.py:1013–1016`, which skips stripping
whenever either tag is absent or malformed, and (b) the routing in
`_process_and_send` (`bot/handlers.py:447–473`) which calls
`_handle_workspace_commands` only on the `is_robyx` branch, so a workspace
agent that emits `[CREATE_CONTINUOUS ...]` lands directly in the user reply.

**Rationale**: Reading `_process_and_send`, only `is_robyx == True` routes into
`_handle_workspace_commands`, the only function that strips the two continuous
tags. `prompt_workspace_agent.md:178` documents the macro format for workspace
agents, so the grammar is advertised in a place the router does not serve.
`_strip_executive_markers` already covers the non-executive collab case, but
the executive workspace-agent case has no hook. Additionally, the `if
cont_match and prog_match` guard is a conjunction: a single malformed tag
disables stripping of both.

**Alternatives considered**:
- Stripping inside `invoke_ai` (before the handler sees the response). Rejected:
  the macro's side effects depend on handler context (`manager`, `platform`,
  `thread_id`, `chat_id`) that `invoke_ai` does not know.
- Teaching each platform adapter to strip the macro before `send_message`.
  Rejected: fan-out across three adapters + TTS + scheduled delivery, and
  violates Principle I (parity by shared code, not by three copies).
- Wrapping only `_handle_workspace_commands` with a broader router. Rejected:
  leaves scheduled-delivery and collaborative paths untouched.

## R-02 — How strict should the regex be?

**Decision**: Loosen `CREATE_CONTINUOUS_PATTERN` and `CONTINUOUS_PROGRAM_PATTERN`
to be case-insensitive on the tag tokens, tolerant of curly quotes in attribute
values (accept `[\u0022\u201C\u201D]` around `name` and `work_dir`), tolerant of
multi-line / extra whitespace inside the tag (`\s+` already covers most, but
allow `\s*` between the tag name and attributes and between attributes). Do
not match tags in the middle of other words (require word boundaries around
the tag name).

**Rationale**: The spec (FR-005, FR-006) explicitly requires tolerance of these
realistic variations. Tightening the model-side prompt is insufficient because
several model backends normalize apostrophes and occasionally emit the tag
inside code fences.

**Alternatives considered**:
- Extend the grammar to also accept JSON5 / trailing commas. Rejected: outside
  the spec scope; the program payload is authored by the LLM which can emit
  strict JSON, and a malformed payload should be reported as an error, not
  silently normalized.
- Support unquoted attributes. Rejected: increases ambiguity (cannot tell where
  the value ends) for negligible benefit.

## R-03 — Stripping policy for partial matches

**Decision**: Every token that is individually detected MUST be stripped from
the user-visible text, regardless of whether its partner tag matched.
Specifically: if only `[CREATE_CONTINUOUS ...]` matched, strip it and emit a
short error ("continuous task not created — program block missing"). If only
`[CONTINUOUS_PROGRAM] ... [/CONTINUOUS_PROGRAM]` matched, strip the whole
block and emit the same error. If the opening `[CONTINUOUS_PROGRAM]` matched
but the closing was missing, strip from the opening tag to end-of-response (or
to the next `[/ANY_TAG]` boundary, whichever comes first), so the JSON cannot
leak.

**Rationale**: SC-004 requires "failure mode is 'not executed', never
'leaked'". The current conjunction-gated stripping makes leaks the default
failure mode; inverting that default is the core fix.

**Alternatives considered**:
- Strip only when both matched (today's behavior). Rejected — this is the bug.
- Strip tokens but leave JSON payload between them. Rejected — the payload
  itself is the most visible leakage (objective text, constraints).

## R-04 — Where in the pipeline should extraction run?

**Decision**: At a single call site inside `_process_and_send`, after
`invoke_ai` returns the fully-assembled response and BEFORE any of the other
marker handlers. This ensures: (i) streaming chunks can never cause a leak
(FR-009); (ii) the workspace-agent branch is covered too (call moves out of
the `is_robyx` conditional); (iii) the TTS pipeline sees the already-scrubbed
text (FR-011).

**Rationale**: `_process_and_send` is the single convergence point for every
agent turn: orchestrator, workspace, and collaborative (executive and
participant). Lifting the call above the `is_robyx` / non-executive split
eliminates three parallel implementations.

**Alternatives considered**:
- Run inside `_handle_workspace_commands`. Rejected: that function is currently
  only reached on the `is_robyx` branch.
- Run inside `_strip_executive_markers`. Rejected: that function already
  strips the tags for non-executive messages; duplicating stripping in
  another place invites divergence.

## R-05 — Scheduled delivery

**Decision**: `bot/scheduled_delivery.py` must hand its outbound text through
`extract_continuous_macros` + `apply_continuous_macros` the same way
`_process_and_send` does, so that a scheduled continuous-setup interview that
ends on a macro does not leak on its final delivery.

**Rationale**: The scheduler late-fires agent replies that were queued while
the process was down (Principle III). Those replies originate from the same
agent turns and can legitimately contain a macro emitted at the interview
boundary.

**Alternatives considered**:
- Assume scheduled replies never contain macros. Rejected: scheduler fans
  agent output through without interception today and there is no invariant
  preventing an interview boundary to land on a scheduled-delivery turn.

## R-06 — Error message surface (localization)

**Decision**: User-visible substitution strings are added to `bot/i18n.py` under
new keys:
- `continuous_task_created` (confirmation — already exists inline; moved into
  i18n for parity)
- `continuous_task_error_malformed`
- `continuous_task_error_bad_json`
- `continuous_task_error_missing_field` (templated with the missing field name)
- `continuous_task_error_path_denied`
- `continuous_task_error_name_taken`
- `continuous_task_error_permission_denied`
- `continuous_task_error_downstream` (generic, includes a short log-ref id)

**Rationale**: The existing inline strings in `handlers.py` are English-only
and violate `tests/test_i18n_parity.py`. Moving them into `i18n.py` also
centralizes copy review.

**Alternatives considered**:
- One generic error string. Rejected: FR-004 asks for a short error naming
  what is missing when a required field is absent. Generic strings fail that.

## R-07 — TTS consistency

**Decision**: TTS rendering already runs on the post-processed response string
(confirmed by inspection of `bot/voice.py` integration in `_send_response`).
No separate hook is needed — placing extraction in `_process_and_send` before
`TTS_SUMMARY_PATTERN.sub` ensures voice and chat see identical scrubbed text.

**Rationale**: Matches FR-011 without adding a second integration point.

**Alternatives considered**:
- Add an explicit re-scrub inside the TTS renderer. Rejected: unnecessary if
  the single upstream scrub is in the right place.

## R-08 — Logging and observability

**Decision**: Each detection emits one `INFO` log line with fields
`agent=<name>, outcome=<created|rejected:<reason>|malformed:<reason>>,
name=<slug or ?>, work_dir=<path or ?>, duration_ms=<n>`. On `created`, also
log `thread_id`, `branch`. Unstructured formatting is fine; the existing
`robyx.handlers` logger is used.

**Rationale**: FR-010 requires that operators can diagnose from logs without
the chat transcript. One log line per macro is enough for the volume.

**Alternatives considered**:
- Emit a structured JSON event to a separate file. Rejected: out of scope;
  Robyx has no structured telemetry pipeline yet.

## R-09 — Backward compatibility of existing tests

**Decision**: The fix is behavior-additive for the golden path (the tests in
`tests/test_continuous.py` and the continuous sections of `tests/test_handlers.py`
remain green). For the malformed-macro tests, any prior test that asserted the
raw macro appears in the response is obsolete and MUST be updated to assert
substitution.

**Rationale**: SC-006 explicitly requires no regression in scheduler hand-off,
parent-workspace attribution, or `work_dir` confinement.

**Alternatives considered**: n/a.

## Summary of Unknowns

There are no remaining `NEEDS CLARIFICATION` markers in Technical Context after
this research pass. Every decision above either cites the spec or is a direct
observation from the current codebase (`bot/handlers.py`, `bot/ai_invoke.py`,
`bot/continuous.py`, `templates/prompt_workspace_agent.md`).
