# Phase 1 — Data Model: Fix Continuous Task Macro Leak

This feature does NOT introduce any new persisted data. The on-disk
`data/continuous/<name>/state.json` format (created by
`bot/continuous.create_continuous_task`) is unchanged. The model below
describes only the in-memory entities that flow through the response
processor so the contracts can reference them by name.

## Entities

### ContinuousMacroTokens

Raw detection output. Produced by the tolerant regex pass over the final
assembled agent response.

| Field             | Type                          | Notes |
|-------------------|-------------------------------|-------|
| `open_span`       | `(int, int)` or `None`        | Start/end offsets of the `[CREATE_CONTINUOUS ...]` tag, or `None` if absent. |
| `program_span`    | `(int, int)` or `None`        | Offsets of the full `[CONTINUOUS_PROGRAM] ... [/CONTINUOUS_PROGRAM]` block, or `None` if absent. |
| `name_raw`        | `str` or `None`               | `name="..."` attribute value as matched (pre-normalization). |
| `work_dir_raw`    | `str` or `None`               | `work_dir="..."` attribute value as matched. |
| `program_raw`     | `str` or `None`               | Text between the `[CONTINUOUS_PROGRAM]` tags, before JSON parse. |
| `surrounding_fence` | `(int, int)` or `None`      | Offsets of a triple-backtick code fence that exists *solely* to wrap the macro; detected so the fences are removed along with the tags. |

Validation rules during detection (NOT during side-effect dispatch):

- Tag tokens match case-insensitively.
- Attribute values may be delimited by `"`, `\u201C\u201D` (curly double), or
  `\u2018\u2019` (curly single).
- An open tag without a program block, or a program block without an open
  tag, is still recorded (its span is populated; its partner is `None`).
- If the opening `[CONTINUOUS_PROGRAM]` is found but its closer is missing,
  `program_span.end` is set to `len(response)` so stripping removes the
  whole remainder.

### ContinuousMacroOutcome

One per detected macro. Either `Intercepted` (successful creation) or
`Rejected` (error). Returned by `apply_continuous_macros` alongside the
stripped response text.

Discriminator: `outcome`.

| Field           | Type                 | When `outcome == "intercepted"` | When `outcome == "rejected"` |
|-----------------|----------------------|---------------------------------|------------------------------|
| `outcome`       | Literal              | `"intercepted"`                 | `"rejected"`                 |
| `name`          | `str`                | The slug created                 | The slug if parseable, else `"?"` |
| `thread_id`     | `Any`                | The new topic/channel id         | `None`                       |
| `branch`        | `str`                | The new git branch               | `None`                       |
| `reason`        | `str`                | `None`                           | Short machine tag (see RejectReason below). |
| `detail`        | `str`                | Optional confirmation detail     | Free-text detail for logs (never shown raw to users). |

### RejectReason (string enum)

Stable identifiers used for logging AND to pick the correct i18n string.

- `malformed_missing_open` — `[CONTINUOUS_PROGRAM]` seen, `[CREATE_CONTINUOUS]` not.
- `malformed_missing_program` — `[CREATE_CONTINUOUS]` seen, program block not.
- `malformed_unclosed_program` — opening `[CONTINUOUS_PROGRAM]` without `[/CONTINUOUS_PROGRAM]`.
- `bad_json` — program block present but JSON parse failed.
- `missing_field` — JSON parsed but a required key (e.g., `objective`) is absent.
- `path_denied` — `work_dir` resolves outside `WORKSPACE`.
- `invalid_work_dir` — `work_dir` is syntactically invalid or unresolvable.
- `name_taken` — slug collides with an existing continuous task.
- `permission_denied` — emitting agent lacks authority in this context.
- `downstream_error` — `create_continuous_workspace` raised unexpectedly.

### ApplyContext

Parameters threaded into `apply_continuous_macros`. Kept as a small
dataclass (or typed dict) to avoid long argument lists at every call site.

| Field          | Type                     | Notes |
|----------------|--------------------------|-------|
| `agent`        | `agents.Agent`            | Emitter — name, role, permissions, collab membership. |
| `thread_id`    | `Any`                     | Source thread the macro was emitted from. Used to resolve `parent_workspace` (falls back to `"robyx"` when no workspace is mapped). |
| `chat_id`      | `Any`                     | For downstream topic creation. |
| `platform`     | `messaging.base.Platform` | For topic / channel creation. |
| `manager`      | `agents.AgentManager`     | For workspace lookup and registration. |
| `is_executive` | `bool`                    | Defense in depth: on `False`, every detected macro is converted to `Rejected(permission_denied)` before any side effect runs. |

### Program (unchanged — existing contract)

Passed through to `topics.create_continuous_workspace` unchanged after JSON
parse + required-field validation. Documented here only for completeness.

| Field               | Required | Type        |
|---------------------|----------|-------------|
| `objective`         | yes      | `str`        |
| `success_criteria`  | yes      | `list[str]`  |
| `constraints`       | no       | `list[str]`  |
| `checkpoint_policy` | no       | `str` (default `"on-demand"`) |
| `first_step`        | yes      | `{number:int, description:str}` |
| `context`           | no       | `str`        |

Validation (existing, reinforced here):

- `objective` non-empty.
- `success_criteria` list with ≥1 entry.
- `first_step.description` non-empty.

## State Transitions

No new persisted state. Macro processing is a pure function of the response
string plus `ApplyContext`; the side effects it triggers
(`create_continuous_workspace`) own their own state transitions unchanged.
