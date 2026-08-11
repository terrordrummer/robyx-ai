# Auto-Updates, Migrations, and Service Management

← [Back to README](../README.md)

## Auto-Updates

Robyx checks for new versions every hour:

- **Safe updates** (non-breaking, compatible) are applied automatically — resolve the exact release tag, install Python dependencies, run post-update migrations, restart
- **Breaking updates** notify you and require manual `/doupdate`
- If anything fails, it rolls back to the exact pre-update commit and then restores `data/` from the verified snapshot
- Use `/checkupdate` for an immediate check

### Transactional update flow

```
1. reject unmerged/in-progress git operations
2. take the exclusive maintenance gate; block new messages/scheduler writes
3. checkpoint SQLite WALs, snapshot data/, and fully verify the archive
4. durably record data/backups/active-update.json before the first mutation
5. git stash --include-untracked, attach to main, and capture its rollback SHA
6. query/fetch only refs/tags/v<VERSION> into an updater-owned ref
7. verify the advertised tag object, peeled commit, VERSION, and release metadata
8. git reset --hard <verified-target-commit>
9. run release migration steps
10. select the current-minor runtime lock and install it with `--require-hashes`
11. smoke-test bot.py, then restore and syntax-check the stash
12. re-verify HEAD and VERSION; invalidate affected AI sessions and record commit
13. apply deferred gateway lifecycle events to the final tree; remove the marker
14. release maintenance and restart_service()
```

The updater never substitutes the tip of `main` for the requested version. This matters when `main` contains unreleased work beyond the latest tag: the installed and recorded commit is always the commit peeled from `v<VERSION>`.

After an update restarts Robyx, the stdlib-only local-security bootstrap runs
before `.env` is loaded. It idempotently restores owner-only modes for `.env`,
`bot.log*`, and the complete `data/` tree, including installations already at
the latest migration tracker version. Install scripts run the same routine
before enabling the service; this filesystem policy intentionally is not tied
to a one-shot schema migration.

Rollback is ordered deliberately. Robyx first resets `main` to the captured pre-update SHA and verifies `HEAD`; only then does it restore the data snapshot. Data restore is prepared in a sibling staging directory and committed with same-filesystem directory renames while the current tree remains in quarantine; a failed swap restores the current tree and keeps the snapshot available. After a successful restore, the live `AgentManager` reloads that authoritative tree, the updater restores the exact original branch or detached commit, and `VERSION` is verified before runtime writes reopen. If any of those checks fail, the recovery marker remains and the maintenance gate fails stopped. If code rollback cannot be proven successful, the updater leaves the snapshot untouched, preserves the local-change stash, and does **not** overlay older data onto unknown code.

Snapshots are mandatory whenever `data/` contains runtime files. A snapshot must be readable end to end and pass path, link, size, and gzip/tar integrity checks before the working tree is changed. Empty clean-shell installs do not create a meaningless snapshot. The three newest verified snapshots are retained.

Manual and automatic updates share one writer transaction. Ordinary invocations,
scheduler cycles, delivery watchers, configuration changes, lifecycle commands,
and collaborative mutations hold shared leases. `/doupdate force` blocks new
leases, terminates and reaps supervised process groups, and waits for their
delivery writers before snapshotting; a non-forced update fails closed while
runtime work is active. If power is lost after the durable transaction marker
is written, the next stdlib bootstrap refuses to run unverified code. Inspect
`data/backups/active-update.json`, restore its recorded commit and optional
snapshot, smoke-test, and remove the marker only after recovery is verified.
During service shutdown, an active update writer is cancelled and given a
bounded rollback window while the child-process supervisor still accepts the
compensating git commands; only after that rollback finishes does normal
supervisor closing and task cancellation begin.

The configured `origin` remote is the updater's trust root. The exact remote tag object and peeled commit are verified for consistency, but tags are not currently required to carry a cryptographic signature.

### Dependency safety net

Auto-update is rigorous about Python dependencies:

- `apply_update` resolves `requirements/locks/runtime-pyXY.txt` for the running Python 3.10–3.14 interpreter and installs it with `--require-hashes`, return-code checking, and a 10-minute timeout. Git, migration, pip, and smoke-test children run in isolated, supervised process groups; timeout, cancellation, or a descendant surviving its leader terminates the whole tree and rejects the phase before rollback. Successful pip output is not logged; failure diagnostics are bounded and redact secret environment values plus URL credentials. Migration commands are withheld from chat/history and run with a strict operational environment allowlist.
- A stdlib-only startup bootstrap check (`bot/_bootstrap.py`) runs before third-party imports. Its marker fingerprints both `bot/requirements.txt` and the selected runtime lock, then performs the same hash-verified install when either changes. Missing pip, a missing or unsupported lock, launch errors, timeouts, signals, and non-zero exits fail closed. Temporary SIGTERM/SIGINT handlers ensure an isolated pip/build tree is terminated and reaped even before the normal bot shutdown hooks exist.
- The macOS, Linux, and Windows installers use the same resolver and runtime locks. Development/test packages live only in the separate `dev-pyXY.txt` locks used by CI and contributors. Declared Python minors never silently fall back to an adjacent or unlocked dependency set.

### Post-update smoke test

After `pip install` succeeds and *before* the success state is recorded, the updater spawns `<venv>/bin/python bot/bot.py --smoke-test` with a 60s timeout. Pip exit 0 isn't enough — a successful resolve can still hide a transitive dependency conflict that only surfaces at import time. Catching that here lets the updater roll back instead of restarting straight into a broken bot.

## Post-update migrations

Migrations run post-update on the next boot after an update. Each successfully
recorded step runs exactly once; failed version-chain steps are retried on a
later boot. Two layers live in `bot/migrations/`:

- **Version chain** (since 0.20.12) — every release ships a matching `bot/migrations/vX_Y_Z.py` module with `from_version` / `to_version` / `upgrade()`. The chain must be continuous: multi-version jumps (e.g. 0.18 → 0.25) run every intermediate step in order. A contract test (`tests/test_migrations_framework.py::TestChainContract`) fails the build if any release is missing its migration. Scaffold a new one with `python scripts/new_migration.py X.Y.Z`.
- **Legacy name-keyed registry** (pre-0.20.12) — kept in `bot/migrations/legacy.py` for backwards compatibility with existing installs; no new migrations are added here.

Both layers are tracked in `data/migrations.json` (chain state lives under the
`_chain_` key), and migrations must be idempotent. Legacy name-keyed migrations
are recorded after their first attempt and are not retried automatically. A
failed version-chain step stops later dependent steps and is retried on the next
boot until it succeeds; successfully recorded chain steps run only once. A
migration error is logged but does not prevent the current bot boot.

## Agent session lifecycle on updates

The Claude Code CLI bakes the system prompt into a session at creation time and ignores `--append-system-prompt` on `--resume`. So whenever a release modifies a system prompt or an agent brief, the affected agents must start a fresh session for the new instructions to actually take effect.

Since v0.15.1 this is **automatic and structural**, and since v0.15.2 it's **also correct in production**. After a successful exact-tag install, `apply_update` computes `git diff --name-only <pre-update-SHA>..HEAD` and hands the changed paths to `bot/session_lifecycle.py:invalidate_sessions_via_manager`, which routes the actual reset through the live `AgentManager.reset_sessions(...)` method:

- A change to `bot/config.py` (the system prompts) or `bot/ai_invoke.py` (the per-agent brief loader) resets **every** agent.
- A change to repo-managed `agents/<name>.md` resets only **that** workspace agent.
- A change to repo-managed `specialists/<name>.md` resets only **that** specialist.
- Anything else (Python logic, tests, README, releases) is correctly ignored — those changes are picked up by the process restart that follows `apply_update`.

**Why "via the manager" matters**: in v0.15.0 and v0.15.1 the reset was implemented as a direct write to `data/state.json`. The running bot's `AgentManager` held the pre-mutation copy in memory and the very next `save_state()` call from any interaction silently overwrote the reset. The migration was tracked as `success` but the agents kept running with the old prompt forever. v0.15.2 fixes this structurally by going through `AgentManager.reset_sessions(...)`, which mutates the in-memory copy and persists in a single atomic step. **`state.json` is never mutated outside the AgentManager**.

The progress callback emits `Reset AI sessions for N agent(s): name1, name2` so the side effect is visible inline in the boot summary on Telegram. Failures here are logged but never block the update — the restart still happens. Release authors no longer need to write per-release session-reset migrations: the contract is anchored in the updater itself.

---

## Service Management

<details>
<summary><strong>macOS (launchd)</strong></summary>

```bash
./install/install-mac.sh              # Install
launchctl start com.robyx.bot       # Start
launchctl stop com.robyx.bot        # Stop (temporary — KeepAlive restarts it)
./install/uninstall-mac.sh            # Uninstall (stops + removes service)
```

The service runs at login with `KeepAlive` enabled — if it crashes or is killed, launchd restarts it automatically. To **permanently stop** the service, use `uninstall-mac.sh` or run `launchctl unload ~/Library/LaunchAgents/com.robyx.bot.plist` (this removes both the keep-alive and the process). Simply killing the process or using `launchctl stop` will only stop it temporarily.

</details>

<details>
<summary><strong>Linux (systemd)</strong></summary>

```bash
./install/install-linux.sh            # Install
systemctl --user start robyx        # Start
systemctl --user stop robyx         # Stop (temporary — Restart=on-failure may restart it)
./install/uninstall-linux.sh          # Uninstall (stops + disables + removes service)
```

The service has `Restart=on-failure` — systemd restarts it after crashes. To **permanently stop**, use `uninstall-linux.sh` or run `systemctl --user disable --now robyx`.

</details>

<details>
<summary><strong>Windows (Task Scheduler)</strong></summary>

```powershell
powershell install/install-windows.ps1          # Install
Start-ScheduledTask -TaskName Robyx           # Start
Stop-ScheduledTask -TaskName Robyx            # Stop
powershell install/uninstall-windows.ps1        # Uninstall (stops + removes task)
```

</details>

A **PID file** (`data/bot.pid`) ensures only one instance runs at a time. If you accidentally start the bot twice, the second instance exits immediately.

All three installers stop the existing launchd service, systemd user service, or
Windows scheduled task and wait up to 30 seconds before clearing the live
virtual environment. If the process does not stop, installation aborts with an
actionable error and leaves the environment untouched. Windows also unregisters
the stopped task before recreating it.

---

← [Back to README](../README.md)
