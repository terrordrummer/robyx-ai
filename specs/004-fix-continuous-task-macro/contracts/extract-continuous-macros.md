# Contract: `continuous_macro` — Extraction and Application API

Module: `bot/continuous_macro.py` (NEW).

## Public surface

```python
# Pure detection + stripping. No I/O.
def extract_continuous_macros(text: str) -> tuple[str, list[ContinuousMacroTokens]]: ...

# Apply side effects and produce the user-visible response.
async def apply_continuous_macros(
    response: str,
    ctx: ApplyContext,
) -> tuple[str, list[ContinuousMacroOutcome]]: ...
```

Types are defined in `data-model.md`.

## `extract_continuous_macros(text) -> (stripped, tokens)`

**Contract**:

- Input: the final assembled agent response (a `str`, not a stream).
- Output: a tuple of
  - `stripped`: the input text with every detected macro span removed, code-fence wrappers removed when the fence contained nothing but the macro, and runs of ≥3 newlines collapsed to 2.
  - `tokens`: zero or more `ContinuousMacroTokens`, in source order.
- Idempotent: `extract(extract(x).stripped).stripped == extract(x).stripped` (applying twice yields no further change).
- Pure: no side effects, no I/O, no logging.
- Ordering: tokens are ordered by the smaller of `open_span.start` and `program_span.start` (whichever is present).

**Must handle**:

1. Perfectly formed single macro (opener + program block).
2. Multiple macros in one string.
3. Case-insensitive tag tokens.
4. Curly quotes around attribute values.
5. Extra whitespace (including newlines) inside the opener.
6. Triple-backtick code fence wrapping the macro.
7. Leading and trailing prose.
8. Opener with no program block → token records `program_span = None`.
9. Program block with no opener → token records `open_span = None`.
10. Opener `[CONTINUOUS_PROGRAM]` with no closer → `program_span.end = len(text)`.

**Must NOT**:

- Call `json.loads` (JSON parsing is done in `apply_continuous_macros`).
- Contact the filesystem or network.
- Log (logging is the caller's responsibility).

## `apply_continuous_macros(response, ctx) -> (response, outcomes)`

**Contract**:

1. Call `extract_continuous_macros(response)` to get `stripped, tokens`.
2. If `tokens` is empty, return `(response, [])` unchanged.
3. Start from `stripped`. For each token in order:
   1. If `ctx.is_executive is False`: produce `Rejected(permission_denied, name=parsed_or_"?")`. Skip side effects.
   2. If `token.open_span is None` or `token.program_span is None` or the program is unclosed: produce the corresponding `malformed_*` rejection.
   3. Parse `token.program_raw` with `json.loads`; on failure → `Rejected(bad_json)`.
   4. Validate required fields per `continuous-macro-grammar.md` → `Rejected(missing_field, detail=<field>)` on failure.
   5. Resolve `work_dir`: if not under `config.WORKSPACE` → `Rejected(path_denied)`; on `OSError/ValueError` → `Rejected(invalid_work_dir)`.
   6. Resolve `parent_workspace` via `ctx.manager.get_by_thread(ctx.thread_id)`, falling back to `"robyx"` (existing rule).
   7. Call `topics.create_continuous_workspace(...)`. On `ValueError("name taken ...")` → `Rejected(name_taken)`. On other `ValueError` → `Rejected(downstream_error, detail=str(e))`. On any other exception → `Rejected(downstream_error)` (caller has already logged).
   8. On success → `Intercepted(name, thread_id, branch)`.
4. Append one user-visible line per outcome to `stripped`, separated by `\n\n`, using the i18n keys below.
5. Return `(new_response, outcomes)`.

**i18n keys** (added to `bot/i18n.py`):

| Outcome                                  | Key                                       | Format args        |
|------------------------------------------|-------------------------------------------|--------------------|
| `Intercepted`                            | `continuous_task_created`                 | `name, topic, branch` |
| `Rejected(malformed_missing_open)`       | `continuous_task_error_malformed`         | —                  |
| `Rejected(malformed_missing_program)`    | `continuous_task_error_malformed`         | —                  |
| `Rejected(malformed_unclosed_program)`   | `continuous_task_error_malformed`         | —                  |
| `Rejected(bad_json)`                     | `continuous_task_error_bad_json`          | —                  |
| `Rejected(missing_field)`                | `continuous_task_error_missing_field`     | `field`            |
| `Rejected(path_denied)`                  | `continuous_task_error_path_denied`       | —                  |
| `Rejected(invalid_work_dir)`             | `continuous_task_error_path_denied`       | —                  |
| `Rejected(name_taken)`                   | `continuous_task_error_name_taken`        | `name`             |
| `Rejected(permission_denied)`            | `continuous_task_error_permission_denied` | —                  |
| `Rejected(downstream_error)`             | `continuous_task_error_downstream`        | —                  |

Messages MUST NOT contain any of: `[CREATE_CONTINUOUS`, `[CONTINUOUS_PROGRAM`, `{`, `}`, or the raw JSON payload.

**Logging**:

- One `INFO` line per outcome: `"continuous.macro outcome=%s name=%s agent=%s ..."`.
- On `downstream_error` or `bad_json`, also log the captured detail at `WARNING` (never shown to the user).

## Call sites (integration contract)

`apply_continuous_macros` MUST be called on every terminal response path before the text reaches any platform adapter or TTS renderer:

- `bot/handlers.py:_process_and_send` — **immediately after** `invoke_ai` returns and **before** any other marker handler (workspace, collab, media, TTS). Runs on both the `is_robyx` and workspace-agent branches.
- `bot/handlers.py:_strip_executive_markers` — unchanged; this path already scrubs the tokens for non-executive participant responses and should continue to do so (defense in depth). `apply_continuous_macros` on those responses will see zero tokens and be a no-op.
- `bot/scheduled_delivery.py` — the outbound late-fire path MUST run the same call on any agent-originated text before `platform.send_message`.
