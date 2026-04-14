# Auto-Updates, Migrations, and Service Management

← [Back to README](../README.md)

## Auto-Updates

Robyx checks for new versions every hour:

- **Safe updates** (non-breaking, compatible) are applied automatically — pull, install Python dependencies, run post-update migrations, restart
- **Breaking updates** notify you and require manual `/doupdate`
- If anything fails, it rolls back automatically to the previous version tag *and* restores `data/` from the pre-update snapshot
- Use `/checkupdate` for an immediate check

### The update flow (since v0.20.14)

```
1. git stash --include-untracked
2. snapshot data/ → data/backups/pre-update-FROM-to-TO-TS.tar.gz   (retention: 3)
3. git pull --ff-only                                              (rollback on fail)
4. parse releases/<version>.md                                     (frontmatter + migration steps)
5. run shell migration steps                                       (rollback code+data on fail)
6. pip install -r bot/requirements.txt                             (rollback on fail)
7. smoke test: python -c "import bot.bot"                          (rollback code+data on fail)
8. session invalidation (diff-driven)                              (best-effort)
9. record success → data/updates.json
10. restart_service()
```

Every rollback path restores both the previous git tag and the data snapshot, so a botched migration cannot leave the next boot reading half-mutated state.

### Dependency safety net

Auto-update is rigorous about Python dependencies:

- `apply_update` runs `pip install -r bot/requirements.txt` with full logging, return-code checking, and a 10-minute timeout. A non-zero pip exit rolls the update back to the previous version and reports the pip error in chat — no silent failures.
- A startup bootstrap check (`bot/_bootstrap.py`) runs at the top of every bot start-up. It hashes `requirements.txt` against a marker stored inside the venv and reruns `pip install` if they differ. This covers manual pulls, crashed updates, and corrupted venvs — any boot with stale deps self-heals before `import`s run.

### Post-update smoke test

After `pip install` succeeds and *before* the success state is recorded, the updater spawns `<venv>/bin/python -c "import bot.bot"` with a 60s timeout. Pip exit 0 isn't enough — a successful resolve can still hide a transitive dependency conflict that only surfaces at import time. Catching that here lets the updater roll back instead of restarting straight into a broken bot.

## Post-update migrations

Migrations run post-update, exactly once per deployment, on the next boot after an update. Two layers live in `bot/migrations/`:

- **Version chain** (since 0.20.12) — every release ships a matching `bot/migrations/vX_Y_Z.py` module with `from_version` / `to_version` / `upgrade()`. The chain must be continuous: multi-version jumps (e.g. 0.18 → 0.25) run every intermediate step in order. A contract test (`tests/test_migrations_framework.py::TestChainContract`) fails the build if any release is missing its migration. Scaffold a new one with `python scripts/new_migration.py X.Y.Z`.
- **Legacy name-keyed registry** (pre-0.20.12) — kept in `bot/migrations/legacy.py` for backwards compatibility with existing installs; no new migrations are added here.

Both layers are tracked in `data/migrations.json` (chain state lives under the `_chain_` key). Migrations are idempotent, never retried on failure, and never block the boot.

## Agent session lifecycle on updates

The Claude Code CLI bakes the system prompt into a session at creation time and ignores `--append-system-prompt` on `--resume`. So whenever a release modifies a system prompt or an agent brief, the affected agents must start a fresh session for the new instructions to actually take effect.

Since v0.15.1 this is **automatic and structural**, and since v0.15.2 it's **also correct in production**. After a successful `git pull`, `apply_update` computes `git diff --name-only <previous>..HEAD` and hands the changed paths to `bot/session_lifecycle.py:invalidate_sessions_via_manager`, which routes the actual reset through the live `AgentManager.reset_sessions(...)` method:

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

---

← [Back to README](../README.md)
