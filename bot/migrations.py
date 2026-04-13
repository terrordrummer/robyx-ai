"""Robyx — migration framework.

Each migration is a small async function that runs exactly once per
deployment, after an update is applied and before the bot starts handling
messages. Migrations are identified by a stable string ID and tracked in
``data/migrations.json``.

Design choices:

- **Run once**: a migration is marked as attempted in the tracker file the
  first time it runs, regardless of outcome. We never retry a migration
  automatically — a migration that cannot succeed (missing permissions,
  missing platform feature, etc.) should not block every subsequent boot.
- **Errors are not fatal**: a raised exception is caught, logged, and the
  migration is recorded as ``error``. The bot keeps booting.
- **Idempotency is the migration's job**: each migration should first check
  whether its work is already done and return ``True`` in that case.
- **Order matters**: migrations run in registration order, which is the
  source-order of the ``@migration(...)`` decorators below.

Each migration receives ``(platform, manager)`` so that migrations
mutating agent sessions can call ``manager.reset_sessions(...)`` and
have the change survive the next ``save_state()`` call. Migrations that
only need the platform (e.g. channel renames) accept ``manager`` and
ignore it.

Usage:

.. code-block:: python

    from migrations import run_pending
    ...
    await run_pending(platform, manager)

"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List

from config import DATA_DIR

log = logging.getLogger("robyx.migrations")

MIGRATIONS_FILE = DATA_DIR / "migrations.json"


@dataclass
class Migration:
    id: str
    description: str
    apply: Callable[[Any, Any], Awaitable[bool]]


_REGISTRY: List[Migration] = []


def migration(id: str, description: str):
    """Decorator: register an async migration function.

    The decorated function must be an async callable taking
    ``(platform, manager)`` and returning ``True`` on success or
    ``False`` on a non-fatal failure (e.g. "we already did it", "the
    platform refused"). Migrations that only need ``platform`` should
    still accept ``manager`` in the signature for uniformity.
    """
    def wrap(fn):
        _REGISTRY.append(Migration(id=id, description=description, apply=fn))
        return fn
    return wrap


def _load_applied() -> dict:
    if not MIGRATIONS_FILE.exists():
        return {}
    try:
        data = json.loads(MIGRATIONS_FILE.read_text())
        if not isinstance(data, dict):
            log.warning(
                "Migrations file %s has unexpected shape, treating as empty",
                MIGRATIONS_FILE,
            )
            return {}
        return data
    except Exception as e:
        log.warning("Cannot read %s: %s — treating as empty", MIGRATIONS_FILE, e)
        return {}


def _save_applied(data: dict) -> None:
    MIGRATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MIGRATIONS_FILE.write_text(json.dumps(data, indent=2))


async def run_pending(platform, manager) -> list[tuple[str, str]]:
    """Run every registered migration whose ID is not yet in the applied list.

    Returns a list of ``(migration_id, status)`` tuples for the migrations
    that were executed this boot (not the ones skipped because already
    applied). Status is one of ``"success"``, ``"failed"``, ``"error"``.

    ``manager`` is the live :class:`AgentManager` so that migrations can
    mutate the in-memory agent state directly. Without this, mutations
    written to ``state.json`` are silently clobbered by the next
    ``save_state()`` call from the running bot — exactly the bug that
    made the v0.15.0 reminder reset ineffective on existing fleets.
    """
    applied = _load_applied()
    executed: list[tuple[str, str]] = []

    for m in _REGISTRY:
        if m.id in applied:
            continue

        log.info("Running migration %s: %s", m.id, m.description)
        entry: dict[str, Any] = {"description": m.description}
        try:
            ok = await m.apply(platform, manager)
            entry["status"] = "success" if ok else "failed"
        except Exception as e:
            log.error("Migration %s raised: %s", m.id, e, exc_info=True)
            entry["status"] = "error"
            entry["error"] = str(e)

        applied[m.id] = entry
        _save_applied(applied)
        executed.append((m.id, entry["status"]))

        if entry["status"] == "success":
            log.info("Migration %s: done", m.id)
        else:
            log.warning(
                "Migration %s recorded as %s — will not retry",
                m.id, entry["status"],
            )

    return executed


def clear_registry_for_tests() -> None:
    """Empty the in-memory registry. Intended for unit tests only."""
    _REGISTRY.clear()


# ── Registered migrations ────────────────────────────────────────────────


@migration(
    id="0.12.1-rename-main-to-command-bridge",
    description="Rename the platform's main channel/topic to 'Command Bridge'",
)
async def _rename_to_command_bridge(platform, manager) -> bool:
    """Historical migration: rename orchestrator's home to "Command Bridge".

    Kept in the registry for installs that have never run it (e.g. fresh
    clones that pull straight to a post-0.14 version). New installs will
    immediately have it superseded by the 0.14.0 rename below — both
    migrations run in registration order on the first boot, leaving the
    channel correctly named "Mission Control".

    ``manager`` is unused: this migration only renames a platform channel.
    """
    return await platform.rename_main_channel(
        display_name="Command Bridge",
        slug="command-bridge",
    )


@migration(
    id="0.14.0-rename-command-bridge-to-headquarters",
    description="Rename orchestrator's home from 'Command Bridge' to 'Headquarters'",
)
async def _rename_to_headquarters(platform, manager) -> bool:
    """Rename the control room from 'Command Bridge' to 'Headquarters'.

    Runs once on the first boot after upgrading to 0.14.0. Telegram uses
    the display name verbatim on the forum's General topic; Discord and
    Slack use the slug ``headquarters`` because their channel names must
    be lowercase and dash-separated.

    Idempotency is delegated to the adapter — calling the rename when the
    channel already has the target name is a no-op on every supported
    platform, so a botched first attempt is safe to retry on the next
    upgrade.

    ``manager`` is unused: this migration only renames a platform channel.
    """
    return await platform.rename_main_channel(
        display_name="Headquarters",
        slug="headquarters",
    )


@migration(
    id="0.15.0-reset-sessions-for-reminder-skill",
    description="Reset agent sessions so the new universal Reminders skill reaches the existing fleet",
)
async def _reset_sessions_for_reminder_skill(platform, manager) -> bool:
    """Force every agent to create a fresh AI-CLI session on its next turn.

    v0.15.0 promoted reminders to a universal skill via a new
    ``[REMIND ...]`` pattern documented in all three system prompts. The
    Claude Code CLI bakes the system prompt into the session at creation
    time and ignores ``--append-system-prompt`` on ``--resume``, so any
    agent whose session pre-existed the upgrade would never see the new
    instructions. This migration regenerates the session bookkeeping for
    every known agent, forcing the next interactive turn to start a new
    session that picks up the v0.15 system prompt cleanly.

    Note: in v0.15.0 / v0.15.1 this migration mutated ``state.json``
    directly. The mutation was silently clobbered by the running
    :class:`AgentManager` on its next ``save_state()`` call, leaving the
    fleet stuck on the old prompt. v0.15.2 re-implements the migration
    via :meth:`AgentManager.reset_sessions` so the in-memory copy and
    the file on disk stay in sync. The new migration
    ``0.15.2-reset-sessions-after-clobber-fix`` below re-runs the reset
    on installs that already marked this one as ``success`` against the
    broken implementation.

    ``platform`` is unused.
    """
    if manager is None:
        log.warning("No AgentManager — skipping reset")
        return True
    reset = manager.reset_sessions(None)
    log.info("Reset AI sessions for %d agents (via manager)", len(reset))
    return True


@migration(
    id="0.15.2-reset-sessions-after-clobber-fix",
    description="Re-run the v0.15.0 session reset now that the AgentManager-clobber bug is fixed",
)
async def _reset_sessions_after_clobber_fix(platform, manager) -> bool:
    """Re-trigger the v0.15.0 session reset on existing fleets.

    The v0.15.0 migration ``0.15.0-reset-sessions-for-reminder-skill``
    mutated ``state.json`` directly. The bot's running
    :class:`AgentManager` had loaded the file before the migration ran
    and overwrote the migration's mutation on its very next
    ``save_state()`` call. Result: the migration was tracked as
    ``success`` in ``data/migrations.json`` but the agents never
    actually had their sessions regenerated — they kept running with
    the pre-v0.15 system prompt forever, the new ``[REMIND]`` pattern
    was never delivered, and reminders silently failed.

    v0.15.2 fixes the structural bug by routing the reset through
    :meth:`AgentManager.reset_sessions`. This migration carries a fresh
    ID so it actually runs on installs whose tracker already lists the
    v0.15.0 migration as ``success`` against the broken implementation.

    Fresh installs that have never seen v0.15.0 will run *both*
    migrations in order — the first via the new code path, the second
    as a no-op (everything is already a fresh session).

    ``platform`` is unused.
    """
    if manager is None:
        log.warning("No AgentManager — skipping reset")
        return True
    reset = manager.reset_sessions(None)
    log.info(
        "v0.15.2 reset AI sessions for %d agents (via manager): %s",
        len(reset), ", ".join(reset),
    )
    return True


# ── v0.19.0 — rename KaelOps to Robyx ────────────────────────────────────


@migration(
    "0.19.0-rename-kaelops-to-robyx",
    "Migrate from KaelOps to Robyx: rename agent, memory dirs, .env, deps hash",
)
async def _migrate_kaelops_to_robyx(platform, manager) -> bool:
    """One-time migration from KaelOps to Robyx.

    Handles:
    1. Rename orchestrator agent ``kael`` → ``robyx`` in state.json
    2. Move memory directory ``data/memory/kael/`` → ``data/memory/robyx/``
    3. Rename workspace memory dirs ``.kaelops/memory/`` → ``.robyx/memory/``
    4. Rename venv deps hash ``.kaelops_deps_hash`` → ``.robyx_deps_hash``
    5. Rename ``KAELOPS_*`` env vars to ``ROBYX_*`` in .env
    6. Reset all sessions (system prompts changed)

    All steps are idempotent — safe to re-run.
    """
    import os
    import shutil
    from pathlib import Path
    from config import PROJECT_ROOT

    changes = []

    # 1. Rename orchestrator agent kael → robyx
    if manager is not None and "kael" in manager.agents:
        old_agent = manager.agents.pop("kael")
        old_agent.name = "robyx"
        manager.agents["robyx"] = old_agent
        manager.save_state()
        changes.append("agent kael → robyx")
    elif manager is not None and "robyx" in manager.agents:
        changes.append("agent already robyx (no-op)")

    # 2. Move data/memory/kael/ → data/memory/robyx/
    old_mem = DATA_DIR / "memory" / "kael"
    new_mem = DATA_DIR / "memory" / "robyx"
    if old_mem.is_dir() and not new_mem.exists():
        shutil.move(str(old_mem), str(new_mem))
        changes.append("memory/kael → memory/robyx")
    elif new_mem.is_dir():
        changes.append("memory/robyx already exists (no-op)")

    # 3. Rename workspace .kaelops/memory/ → .robyx/memory/
    if manager is not None:
        for agent in manager.agents.values():
            if agent.agent_type not in ("workspace", "specialist"):
                continue
            work_dir = Path(agent.work_dir)
            old_ws = work_dir / ".kaelops" / "memory"
            new_ws = work_dir / ".robyx" / "memory"
            if old_ws.is_dir() and not new_ws.exists():
                new_ws.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_ws), str(new_ws))
                changes.append(".kaelops → .robyx in %s" % agent.name)
                # Clean up empty .kaelops dir
                old_parent = work_dir / ".kaelops"
                if old_parent.is_dir() and not any(old_parent.iterdir()):
                    old_parent.rmdir()

    # 4. Rename venv deps hash
    venv_dir = PROJECT_ROOT / ".venv"
    old_hash = venv_dir / ".kaelops_deps_hash"
    new_hash = venv_dir / ".robyx_deps_hash"
    if old_hash.exists() and not new_hash.exists():
        old_hash.rename(new_hash)
        changes.append("deps hash renamed")

    # 5. Rename KAELOPS_* → ROBYX_* in .env
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        content = env_file.read_text()
        renames = {
            "KAELOPS_BOT_TOKEN": "ROBYX_BOT_TOKEN",
            "KAELOPS_CHAT_ID": "ROBYX_CHAT_ID",
            "KAELOPS_OWNER_ID": "ROBYX_OWNER_ID",
            "KAELOPS_PLATFORM": "ROBYX_PLATFORM",
            "KAELOPS_WORKSPACE": "ROBYX_WORKSPACE",
        }
        new_content = content
        for old_key, new_key in renames.items():
            if old_key in new_content and new_key not in new_content:
                new_content = new_content.replace(old_key, new_key)
        if new_content != content:
            env_file.write_text(new_content)
            changes.append(".env vars renamed")
        else:
            changes.append(".env already migrated (no-op)")

    # 6. Reset all sessions (prompts changed)
    if manager is not None:
        reset = manager.reset_sessions(None)
        changes.append("sessions reset (%d agents)" % len(reset))

    log.info("kaelops→robyx migration: %s", "; ".join(changes) or "nothing to do")
    return True
