# Contract: Continuous-Task Macro Grammar

**Producer**: Any AI agent (orchestrator, workspace, focused, collaborative executive). Non-executive collab participants MUST NOT emit this macro.

**Consumer**: `bot/continuous_macro.py` (detection + stripping); `bot/topics.create_continuous_workspace` (side effects).

## Tag grammar (relaxed from current implementation)

The macro consists of an **opener** and a **program block**. Both are required for
a successful creation, but each is detected and stripped independently.

### Opener

```
[CREATE_CONTINUOUS name="<slug>" work_dir="<absolute-path>"]
```

Rules:

- Tag token is `CREATE_CONTINUOUS`, matched **case-insensitively**.
- Attribute order is fixed: `name` first, `work_dir` second. (No change from today — reduces ambiguity.)
- Attribute delimiters: any of `"` (U+0022), `\u201C\u201D` (curly double), `\u2018\u2019` (curly single). Mixed pairs are tolerated.
- Whitespace between tag name and attributes, and between attributes, matches `\s+` (supports line breaks).
- `name` MUST match `^[a-z0-9][a-z0-9-]{0,63}$` after case-fold. Other names are rejected with `malformed_missing_program` or, if the program block is also present, with `name_taken`/validation as today.

### Program block

```
[CONTINUOUS_PROGRAM]
{ ...JSON... }
[/CONTINUOUS_PROGRAM]
```

Rules:

- Tag tokens matched case-insensitively.
- Body between the tags is parsed with `json.loads` after `.strip()`.
- An opener `[CONTINUOUS_PROGRAM]` with no closer produces a "unclosed_program" rejection; stripping extends to end-of-response so the JSON cannot leak.

### Payload shape

```json
{
  "objective":         "<string, non-empty>",
  "success_criteria":  ["<string>", ...],
  "constraints":       ["<string>", ...],
  "checkpoint_policy": "<string, e.g. 'every 3 steps' or 'on-demand'>",
  "context":           "<string, optional>",
  "first_step": {
    "number":      1,
    "description": "<string, non-empty>"
  }
}
```

Required keys: `objective`, `success_criteria` (len ≥ 1), `first_step.description`.

## Stripping contract (unconditional)

- Every detected token is removed from the user-visible text, **whether or not** its partner was detected, and **whether or not** its payload parsed successfully.
- If the macro is wrapped in a triple-backtick code fence that contains nothing else, the fences are removed along with the tags.
- Stripping collapses runs of ≥3 newlines to 2 (existing normalization rule).

## Substitution contract (always a prose line, never raw tags)

For each detected macro the processor appends exactly one short line to the user-visible response:

- On success: a **confirmation** referencing `name`, `topic`, and `branch` only.
- On error: a **short prose error** from the i18n table (see `bot/i18n.py` keys `continuous_task_error_*`). The error line MUST NOT contain `[CREATE_CONTINUOUS`, `[CONTINUOUS_PROGRAM`, curly brace JSON, or internal identifiers other than the macro's declared `name` (if parseable).

## Multiple-macro contract

A response MAY contain multiple macros. Each is processed independently and in source order:

- Each successful macro produces one confirmation line.
- Each failing macro produces one error line.
- Ordering of appended lines follows detection order.
- A later macro's failure MUST NOT undo side effects of an earlier successful one.
