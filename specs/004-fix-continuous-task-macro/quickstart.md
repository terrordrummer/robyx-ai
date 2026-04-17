# Quickstart — Continuous-Task Macro Fix

This quickstart describes how to validate the fix locally, both as a developer
running the test suite and as an operator running the live bot.

## 1. Run the test suite

From the repository root:

```bash
cd /Users/rpix/Workspace/products/robyx-ai
python -m pytest tests/test_continuous_macro.py tests/test_continuous.py tests/test_handlers.py tests/test_scheduled_delivery.py -q
```

Expected: all four files pass, including the new fixture-driven cases
enumerated in `specs/004-fix-continuous-task-macro/contracts/*` and the
existing regression tests.

To run the full suite:

```bash
python -m pytest -q
```

## 2. Manual smoke — golden path

1. Start the bot with a single platform enabled (Telegram is fastest for
   interactive testing):

   ```bash
   cd /Users/rpix/Workspace/products/robyx-ai
   python -m bot
   ```

2. In a workspace topic, ask the agent to set up a continuous task, e.g.:
   *"set up a continuous task to keep improving the deconvolution benchmark
   until p95 < 500 ms"*.

3. Complete the interview. When the agent finishes, verify:
   - The message you receive contains a single line confirming the new
     task name, the new topic reference, and the new branch.
   - The message contains **no** `[CREATE_CONTINUOUS`, `[CONTINUOUS_PROGRAM`,
     or JSON braces.
   - `data/continuous/<slug>/state.json` exists and its `program.objective`
     matches the interview.
   - A git branch named `continuous/<slug>` exists in the working directory.
   - The scheduler log shows the first step dispatch for the task.

## 3. Manual smoke — malformed

Use the included helper (added under `tests/fixtures/` as part of this
feature) to replay a synthetic malformed response through the processor:

```bash
python -m bot._dev.replay_continuous_macro tests/fixtures/continuous_macros/bad_json.txt
```

Expected: stdout shows the user-visible substitution (a single short prose
line) and the log line records `outcome=rejected reason=bad_json`. No raw
tokens or JSON appear in the output.

Repeat for each fixture file:

- `missing_program.txt` → `outcome=rejected reason=malformed_missing_program`
- `missing_open.txt` → `outcome=rejected reason=malformed_missing_open`
- `unclosed_program.txt` → `outcome=rejected reason=malformed_unclosed_program`
- `bad_json.txt` → `outcome=rejected reason=bad_json`
- `missing_field_objective.txt` → `outcome=rejected reason=missing_field`
- `path_escape.txt` → `outcome=rejected reason=path_denied`
- `multiple_macros_mixed.txt` → 1× `created`, 1× `rejected reason=bad_json`.

## 4. Manual smoke — realistic variations

Each of these fixtures is a single well-formed macro dressed in a realistic
variation. All MUST produce `outcome=intercepted` and zero leakage:

- `code_fenced.txt` — wrapped in triple backticks.
- `curly_quotes.txt` — attribute values use `\u201C\u201D`.
- `leading_prose.txt` — 150 words of prose precede the macro.
- `mixed_case.txt` — tag names in mixed case (`[Create_Continuous ...]`).
- `extra_whitespace.txt` — newlines between attributes.

## 5. Production verification

After deploying the fix:

1. Tail the bot log and filter for `continuous.macro`:
   ```bash
   journalctl -u robyx.service -f | grep continuous.macro
   ```
2. Observe that every line is a complete `outcome=... name=... agent=...` record.
3. Spot-check the first few continuous-task creations in the chat transcript:
   the reply MUST be a single prose confirmation. If any raw `[CREATE_CONTINUOUS`
   or `[CONTINUOUS_PROGRAM` token appears in the chat, capture the full agent
   response from the log and open a ticket — this is a release blocker.

## 6. Rollback

The fix does not touch persisted state or migrations, so rollback is a plain
tag revert:

```bash
git revert <fix-commit>
systemctl restart robyx
```

Any continuous tasks created while the fix was live continue to run; their
state files are schema-compatible with pre-fix versions of the bot.
