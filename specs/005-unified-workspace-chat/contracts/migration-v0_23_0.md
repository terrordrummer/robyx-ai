# Contract — Migration v0_23_0

## Purpose

Migrate every pre-existing continuous task from the legacy sub-topic delivery model to the parent-workspace-chat delivery model, in a single idempotent pass, with best-effort closure of legacy sub-topics.

## Module

`bot/migrations/v0_23_0.py`, scaffolded via `scripts/new_migration.py`, following the `Migration` dataclass contract in `bot/migrations/base.py`.

```python
MIGRATION = Migration(
    from_version="0.22.2",
    to_version="0.23.0",
    description="unify scheduled/continuous task I/O on parent workspace chat",
    upgrade=upgrade,
    # downgrade intentionally omitted — forward-only
)
```

## Inputs

- `ctx: MigrationContext` with:
  - `ctx.platform`: live platform adapter (may be None in offline/test runs — guard)
  - `ctx.data_dir`: `Path` to `data/`
  - `ctx.log`: logger
- `data/continuous/<name>/state.json` files for every pre-existing continuous task
- `data/workspaces.json` (or equivalent) to resolve parent workspace `thread_id` by `workspace_name`

## Algorithm (per task)

```text
for each state_file in data/continuous/*/state.json:
    state = load_json(state_file)

    # Idempotency: skip already-migrated tasks
    if state.get("migrated_v0_23_0"):
        log.info("skip %s (already migrated)", state["name"])
        continue

    # Resolve parent workspace
    ws = lookup_workspace(state.get("workspace_name") or state.get("parent_workspace_name"))
    if ws is None:
        log.error("unresolved workspace for %s — skipping", state["name"])
        skipped.append(state["name"])
        continue

    legacy_thread_id = state.get("thread_id")
    new_thread_id    = ws["thread_id"]

    # Repoint in-memory
    state["legacy_thread_id"] = legacy_thread_id
    state["thread_id"]        = new_thread_id
    state["plan_path"]        = ensure_plan_md(state)   # migrate inline plan if present; else write stub
    state["migrated_v0_23_0"] = iso_utc_now()

    # Best-effort close of legacy sub-topic
    closed = False
    if ctx.platform is not None and legacy_thread_id is not None and legacy_thread_id != new_thread_id:
        try:
            closed = await ctx.platform.close_channel(legacy_thread_id)
        except Exception as exc:
            log.warning("close_channel failed for %s: %s", state["name"], exc)

        if not closed:
            # Fallback: post a final notice in the legacy sub-topic
            try:
                await ctx.platform.send_to_channel(
                    legacy_thread_id,
                    "🔄 [%s] migrato nel workspace chat." % state["name"],
                )
            except Exception as exc:
                log.warning("fallback notice failed for %s: %s", state["name"], exc)

    # Persist state atomically
    atomic_write(state_file, state)

    # Post one-time transition notice in parent workspace (guarded against dup)
    if ctx.platform is not None:
        try:
            await ctx.platform.send_message(
                chat_id=ws["chat_id"],
                thread_id=new_thread_id,
                text="🔄 [%s] migrato — da ora riporto qui." % state["name"],
            )
        except Exception as exc:
            log.warning("transition notice failed for %s: %s", state["name"], exc)

    migrated.append(state["name"])

# After loop
write_done_marker(ctx.data_dir / "migrations" / "v0_23_0.done")
log.info("migration v0_23_0: migrated=%d skipped=%d", len(migrated), len(skipped))
```

## Idempotency guarantees

- Per-state marker `migrated_v0_23_0` is checked BEFORE any side-effect on a given task → re-running skips already-migrated entries.
- The process-wide `data/migrations/v0_23_0.done` file is checked by the runner (`bot/migrations/runner.py`) — if present, the module is not re-invoked at all.
- Transition notice in the parent workspace is only posted during the first migration run per task (guarded by the marker).
- `close_channel` is called only when `legacy_thread_id != new_thread_id` AND marker is absent → no double-close on re-runs.

## Failure modes

| Failure | Behavior |
|---------|----------|
| Workspace not resolvable | Skip task, log ERROR, include name in summary |
| `close_channel` returns False | Fall back to posting final notice in legacy sub-topic; continue |
| `send_to_channel` for fallback notice raises | Log WARN; continue (marker still stamped so state is consistent) |
| Atomic state write fails | Log ERROR; do NOT stamp marker (so re-run retries); raise to abort the migration |
| Transition notice in parent workspace fails | Log WARN; do NOT roll back marker (we don't want a second notice on re-run) |

## Offline / test mode

When `ctx.platform is None` (offline migration, unit tests), all platform-side steps (`close_channel`, notices) are SKIPPED. State is still repointed and marker still stamped. This lets pytest run the migration against fixture data without a live platform.

## Test assertions

- Fixture with 3 continuous tasks (2 resolvable, 1 with unknown workspace) → after migration: 2 migrated, 1 skipped, exactly 2 transition notices posted in fake platform, exactly 2 `close_channel` calls.
- Re-run on same fixture → 0 new notices, 0 new `close_channel` calls, all markers unchanged.
- Corrupted state JSON → logged error, other tasks still processed, migration returns normally.
- `legacy_thread_id == new_thread_id` (edge: task already pointed at parent) → no `close_channel` called, no fallback notice in legacy sub-topic, transition notice still posted once (protected by marker on re-runs).

## Release artifacts

- `VERSION`: `0.22.2` → `0.23.0`
- `CHANGELOG.md`: entry under `## [0.23.0] - 2026-04-XX` referencing FR-001 through FR-018 at summary level
- `releases/v0.23.0.md`: release notes file with user-facing changes, migration summary, rollback guidance (manual only — no automated downgrade)
