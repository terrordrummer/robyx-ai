# Quickstart — Unified Workspace Chat

End-to-end walkthrough to validate this feature locally before merge.

## Prerequisites

- Robyx checkout on branch `005-unified-workspace-chat`
- Python env set up (`pip install -r bot/requirements.txt -r tests/requirements-test.txt`)
- At least one platform adapter configured with valid credentials (Telegram recommended for forum-topic coverage)
- A pre-existing `data/continuous/*/state.json` from the 0.22.x schema to exercise migration (or a synthetic fixture, see §Seed fixtures)

## 1. Run the focused test suite

```bash
pytest tests/test_scheduled_delivery_markers.py \
       tests/test_continuous.py \
       tests/test_continuous_macro.py \
       tests/test_lifecycle_macros.py \
       tests/test_migration_v0_23_0.py -v
```

All tests MUST pass. Any skipped test MUST carry a `@pytest.mark.skip(reason=...)` with an explicit justification.

## 2. Seed fixtures for migration (optional)

If you don't have pre-existing continuous state, create one:

```bash
mkdir -p data/continuous/quickstart-task
cat > data/continuous/quickstart-task/state.json <<'JSON'
{
  "name": "quickstart-task",
  "status": "paused",
  "objective": "smoke-test migration",
  "chat_id": 1234567890,
  "thread_id": 999,
  "workspace_name": "ops",
  "work_dir": "/tmp/quickstart",
  "branch": "continuous/quickstart-task",
  "history": [],
  "created_at": "2026-03-01T09:00:00Z"
}
JSON
```

Ensure `data/workspaces.json` contains a workspace named `ops` with a real `thread_id`.

## 3. Run the migration offline

```bash
python -m bot.migrations.runner --target 0.23.0 --dry-run
python -m bot.migrations.runner --target 0.23.0
```

Expected:
- Logs: `migration v0_23_0: migrated=<N> skipped=0`
- `data/continuous/quickstart-task/state.json` now contains `migrated_v0_23_0`, `legacy_thread_id=999`, `thread_id=<ops thread>`, `plan_path=data/continuous/quickstart-task/plan.md`
- `data/continuous/quickstart-task/plan.md` exists (stub content if original plan was absent)
- `data/migrations/v0_23_0.done` exists

Re-run and verify idempotency (no new log lines about "migrated", `skipped` bump).

## 4. Live end-to-end — new continuous task

Start the bot:

```bash
./scripts/run.sh   # or whatever launches bot/bot.py on your setup
```

In the workspace chat, ask the primary agent to set up a continuous task (natural language). Confirm the plan when it presents it. Then:

- Verify in your platform client: **no new sub-topic appears**. All activity stays in the workspace chat.
- Within 2 scheduler ticks (~2 min), a message starting with `🔄 [<name>]` arrives in the workspace chat — this is the first step output.
- `data/continuous/<name>/state.json` exists with the parent workspace's `thread_id`.
- `data/continuous/<name>/plan.md` exists with the captured plan.
- The chat message does NOT contain raw `[CREATE_CONTINUOUS ...]` or `[CONTINUOUS_PROGRAM] ... [/CONTINUOUS_PROGRAM]` tokens.

## 5. Lifecycle commands

In the same workspace chat:

- `lista task` → primary responds with icon-grouped summary (🔄 / ⏰ / 📌 / 🔔).
- `stato <name>` → detailed status for the named task.
- `ferma <substring-matching-two-tasks>` → primary asks which one (numbered list), user replies with `1` or full name, task is stopped.
- `pausa <name>` → task transitions to `paused`; scheduler skips it on subsequent ticks.
- `ripristina <name>` → task resumes; next tick picks it up.

Verify log file contains one INFO line per lifecycle action with `{workspace, macro, name, resolved_to, outcome}`.

## 6. Marker sanity across task types

Seed one of each:
- A continuous task (from step 4)
- A periodic task via chat (e.g., "ogni ora controlla X")
- A one-shot via chat (e.g., "domani alle 9 fai Y")
- A reminder via chat (e.g., "ricordami tra 5 minuti di Z")

Wait for deliveries and verify:
- Continuous output: `🔄 [<name>] …`
- Periodic output: `⏰ [<name>] …`
- One-shot output: `📌 [<name>] …`
- Reminder: `🔔 [<name-or-id>] …`

No conversational reply from the primary agent carries any of these markers.

## 7. Migration end-to-end (live)

With a pre-existing task still pointing at its legacy sub-topic:

- Run bot → migration fires on startup via the runner.
- Parent workspace chat receives exactly one transition notice: `🔄 [<name>] migrato — da ora riporto qui.`
- Legacy sub-topic is closed (Telegram: icon changes to "closed"; Discord: archived; Slack: archived) OR carries a final notice if close is unsupported.
- Next tick's output lands in the parent workspace chat.

Restart the bot → no duplicate transition notice, no re-close attempt, logs show "already migrated" per task.

## 8. Failure injection

- Kill the bot mid-migration (after marker set on task A but before task B) → restart → only task B is migrated; task A is untouched.
- Corrupt one state.json by removing the closing brace → migration logs ERROR for that task and continues with the rest; the runner completes.
- Temporarily remove a workspace from `data/workspaces.json` → its tasks are skipped with ERROR in the summary and remain in legacy state for a later retry.

## 9. Rollback guidance

v0_23_0 is forward-only. To roll back manually:

1. Stop the bot.
2. Restore `data/continuous/<name>/state.json` from your pre-migration backup, OR copy `legacy_thread_id` back into `thread_id` and delete the `migrated_v0_23_0`, `plan_path`, `legacy_thread_id` fields by hand.
3. Delete `data/migrations/v0_23_0.done`.
4. Checkout the previous tag (`git checkout v0.22.2`) and restart.

## Definition of done (per spec success criteria)

- [ ] First continuous step lands in workspace chat within 2 ticks, no sub-topic created (SC-001)
- [ ] Zero macro leaks in user-visible chat across all paths (SC-002)
- [ ] Lifecycle commands respond within 5s p95 (SC-003)
- [ ] Ambiguous commands always clarify (SC-004)
- [ ] All pre-existing continuous tasks migrated on first run (SC-005)
- [ ] Re-run idempotent (SC-006)
- [ ] 100% action/log coverage for lifecycle + delivery (SC-007)
- [ ] Zero "silent task" reports after one-week observation (SC-008)
