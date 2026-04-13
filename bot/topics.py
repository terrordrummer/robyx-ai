"""Robyx — Dynamic topic/channel management.

Handles creating, closing, and managing channels (Telegram forum topics, etc.)
for workspaces and specialists via the Platform abstraction.
"""

import logging
import re

from agents import AgentManager
from config import AGENTS_DIR, SPECIALISTS_DIR, SPECIALISTS_FILE, DATA_DIR
from scheduler import (
    FREQUENCY_SECONDS,
    add_task as _add_task,
    cancel_tasks_for_agent_file as _cancel_tasks_for_agent_file,
    validate_one_shot_scheduled_at as _validate_one_shot_scheduled_at,
)

log = logging.getLogger("robyx.topics")

# Reserved names that must never be used for a workspace or specialist:
# - ``robyx`` / ``orchestrator`` would overwrite the Principal Orchestrator
#   entry in AgentManager and brick the bot.
# - The empty string is what ``_sanitize_task_name`` returns for inputs
#   made entirely of punctuation — we must refuse those so we never write
#   ``data/agents/.md`` or register a nameless agent.
RESERVED_AGENT_NAMES = frozenset({"robyx", "orchestrator", ""})


def _sanitize_task_name(name: str) -> str:
    """Convert a display name to a safe task/file name."""
    return re.sub(r'[^a-z0-9-]', '-', name.lower().strip()).strip('-')


def _validate_new_agent_name(safe_name: str, manager: AgentManager, kind: str) -> None:
    """Raise ``ValueError`` if *safe_name* is reserved or already taken.

    *kind* is ``"workspace"`` or ``"specialist"`` — only used in the error
    message so the user sees which operation was rejected. Called before
    any filesystem or channel side effects, so a rejection leaves no
    partial state behind.
    """
    if safe_name in RESERVED_AGENT_NAMES:
        raise ValueError(
            "cannot create %s '%s': name is reserved" % (kind, safe_name or "<empty>")
        )
    if manager.get(safe_name):
        raise ValueError(
            "cannot create %s '%s': name is already in use" % (kind, safe_name)
        )


def _validate_table_safe_display_name(display_name: str, kind: str) -> str:
    """Reject display names that would corrupt the markdown-table stores."""
    value = str(display_name or "").strip()
    if not value:
        raise ValueError("cannot create %s: display name is empty" % kind)
    if any(ch in value for ch in ("|", "\n", "\r")):
        raise ValueError(
            "cannot create %s '%s': display name contains unsupported table characters"
            % (kind, value)
        )
    return value


async def create_workspace(
    name: str,
    task_type: str,
    frequency: str,
    model: str,
    scheduled_at: str,
    instructions: str,
    manager: AgentManager,
    work_dir: str,
    platform=None,
) -> dict | None:
    """Full workspace creation: channel + agent file + tasks.md entry + agent registration.

    Returns dict with workspace info or None on failure.
    """
    display_name = _validate_table_safe_display_name(name, "workspace")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "workspace")
    normalized_scheduled_at = scheduled_at
    if task_type == "one-shot":
        normalized_scheduled_at = _validate_one_shot_scheduled_at(
            scheduled_at,
            label="one-shot workspaces",
        )

    # 1. Create channel/topic
    thread_id = await platform.create_channel(display_name)
    if not thread_id:
        return None

    # 2. Write agent instructions file
    agent_file = AGENTS_DIR / ("%s.md" % safe_name)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Inject config into instructions
    full_instructions = "# %s\n\n%s\n" % (display_name, instructions.strip())
    agent_file.write_text(full_instructions)
    log.info("Wrote agent instructions: %s", agent_file)

    # 3. Register the task in the unified queue
    if task_type == "one-shot":
        _add_task({
            "name": safe_name,
            "agent_file": "agents/%s.md" % safe_name,
            "prompt": "",
            "type": "one-shot",
            "scheduled_at": normalized_scheduled_at,
            "model": model,
            "thread_id": str(thread_id),
            "description": display_name,
        })
    elif task_type == "scheduled":
        freq_str = frequency if frequency != "none" else "hourly"
        interval = FREQUENCY_SECONDS.get(freq_str, 3600)
        from datetime import datetime, timezone
        _add_task({
            "name": safe_name,
            "agent_file": "agents/%s.md" % safe_name,
            "type": "periodic",
            "interval_seconds": interval,
            "next_run": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "thread_id": str(thread_id),
            "description": display_name,
        })
    # interactive workspaces don't go in the queue — agent-only

    # 4. Create data directory
    (DATA_DIR / safe_name).mkdir(parents=True, exist_ok=True)

    # 5. Register agent in manager
    agent = manager.add_agent(
        name=safe_name,
        work_dir=work_dir,
        description=display_name,
        agent_type="workspace",
        model=model,
        thread_id=thread_id,
    )

    # 6. Send welcome message to the new channel
    await platform.send_to_channel(
        thread_id,
        "*%s* workspace is ready.\nAgent *%s* is assigned to this channel."
        % (display_name, safe_name),
    )

    return {
        "name": safe_name,
        "display_name": display_name,
        "thread_id": thread_id,
        "agent_file": str(agent_file),
        "type": task_type,
    }


async def close_workspace(name: str, manager: AgentManager, platform=None) -> bool:
    """Close a workspace: cancel queue entries, close channel, remove agent."""
    agent = manager.get(name)
    if not agent:
        return False

    # Close channel/topic
    if agent.thread_id:
        await platform.send_to_channel(agent.thread_id, "Workspace *%s* closed." % name)
        await platform.close_channel(agent.thread_id)

    canceled = _cancel_tasks_for_agent_file(
        "agents/%s.md" % agent.name,
        reason="workspace closed",
    )
    if canceled:
        log.info(
            "Closed workspace '%s' and canceled %d pending task(s)",
            agent.name,
            canceled,
        )

    # Remove from agent manager
    manager.remove_agent(name)
    return True


async def create_continuous_workspace(
    name: str,
    program: dict,
    work_dir: str,
    parent_workspace: str,
    model: str,
    manager: AgentManager,
    platform=None,
) -> dict | None:
    """Create a continuous task workspace: topic + branch + state + queue entry.

    Returns dict with workspace info or None on failure.
    """
    from continuous import create_continuous_task, state_file_path

    display_name = _validate_table_safe_display_name(name, "continuous workspace")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "continuous workspace")

    branch = "continuous/%s" % safe_name

    # 1. Create channel/topic
    topic_name = "🔄 %s" % display_name
    thread_id = await platform.create_channel(topic_name)
    if not thread_id:
        return None

    # 2. Write agent instructions
    agent_file = AGENTS_DIR / ("%s.md" % safe_name)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    setup_template_path = __import__("pathlib").Path(__file__).parent.parent / "templates" / "CONTINUOUS_SETUP.md"
    if setup_template_path.exists():
        setup_instructions = setup_template_path.read_text()
    else:
        setup_instructions = "You are a continuous task agent."
    full_instructions = "# %s (Continuous Task)\n\n%s\n" % (display_name, setup_instructions)
    agent_file.write_text(full_instructions)

    # 3. Create state file
    state = create_continuous_task(
        name=safe_name,
        parent_workspace=parent_workspace,
        program=program,
        thread_id=thread_id,
        branch=branch,
        work_dir=work_dir,
    )

    # 4. Create data directory
    (DATA_DIR / safe_name).mkdir(parents=True, exist_ok=True)

    # 5. Add to unified queue
    _add_task({
        "name": safe_name,
        "type": "continuous",
        "agent_file": "agents/%s.md" % safe_name,
        "model": model,
        "thread_id": str(thread_id),
        "state_file": str(state_file_path(safe_name)),
        "description": "Continuous: %s" % display_name,
    })

    # 6. Register agent
    agent = manager.add_agent(
        name=safe_name,
        work_dir=work_dir,
        description="[Continuous] %s" % display_name,
        agent_type="workspace",
        model=model,
        thread_id=thread_id,
    )

    # 7. Welcome message
    await platform.send_to_channel(
        thread_id,
        "*🔄 %s* continuous workspace is ready.\n"
        "Agent *%s* will work autonomously on branch `%s`.\n\n"
        "**Objective:** %s\n\n"
        "Send a message here to interrupt and interact."
        % (display_name, safe_name, branch, program.get("objective", "N/A")),
    )

    return {
        "name": safe_name,
        "display_name": display_name,
        "thread_id": thread_id,
        "branch": branch,
        "state_file": str(state_file_path(safe_name)),
        "type": "continuous",
    }


async def create_specialist(
    name: str,
    model: str,
    instructions: str,
    manager: AgentManager,
    work_dir: str,
    platform=None,
) -> dict | None:
    """Create a cross-functional specialist agent."""
    display_name = _validate_table_safe_display_name(name, "specialist")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "specialist")

    # 1. Create channel/topic
    thread_id = await platform.create_channel("Specialist: %s" % display_name)
    if not thread_id:
        return None

    # 2. Write specialist instructions
    SPECIALISTS_DIR.mkdir(parents=True, exist_ok=True)
    spec_file = SPECIALISTS_DIR / ("%s.md" % safe_name)
    full_instructions = "# %s (Cross-functional Specialist)\n\n%s\n" % (
        display_name, instructions.strip(),
    )
    spec_file.write_text(full_instructions)

    # 3. Append to specialists.md
    row = "| %s | specialists/%s.md | %s | %s | %s |\n" % (
        safe_name, safe_name, model, thread_id, display_name,
    )
    _append_to_specialists(row)

    # 4. Register agent
    agent = manager.add_agent(
        name=safe_name,
        work_dir=work_dir,
        description="[Specialist] %s" % display_name,
        agent_type="specialist",
        model=model,
        thread_id=thread_id,
    )

    # 5. Welcome message
    await platform.send_to_channel(
        thread_id,
        "*%s* specialist is ready.\nAvailable across all workspaces via `@%s`."
        % (display_name, safe_name),
    )

    return {
        "name": safe_name,
        "display_name": display_name,
        "thread_id": thread_id,
    }


def _update_queue_entry_thread_id(name: str, thread_id) -> None:
    """Update the thread_id for a task in queue.json."""
    from scheduler import load_queue, save_queue
    entries = load_queue()
    for entry in entries:
        if entry.get("name") == name:
            entry["thread_id"] = str(thread_id) if thread_id is not None else ""
    save_queue(entries)


def _append_to_specialists(row: str):
    """Append a row to specialists.md, creating the file if needed."""
    SPECIALISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SPECIALISTS_FILE.exists():
        header = (
            "| Agent | Instructions | Model | Thread ID | Description |\n"
            "|-------|-------------|-------|-----------|-------------|\n"
        )
        SPECIALISTS_FILE.write_text(header + row)
    else:
        with open(SPECIALISTS_FILE, "a") as f:
            f.write(row)


# ── Healing detached workspaces ────────────────────────────────────────────


def _update_table_thread_id(path, name: str, column_index: int, thread_id: int | None) -> None:
    """Rewrite the Thread ID column for *name* in a markdown table file."""
    if not path.exists():
        return

    replacement = "-" if thread_id is None else str(thread_id)
    new_lines: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cols = [c.strip() for c in stripped.split("|")[1:-1]]
            if cols and cols[0] == name and len(cols) > column_index:
                cols[column_index] = replacement
                line = "| %s |" % " | ".join(cols)
        new_lines.append(line)
    path.write_text("\n".join(new_lines) + "\n")


def _update_specialist_thread_id(name: str, thread_id: int | None):
    """Rewrite the Thread ID column for *name* in specialists.md (column index 3)."""
    _update_table_thread_id(SPECIALISTS_FILE, name, 3, thread_id)


async def heal_detached_workspaces(manager: AgentManager, platform=None) -> list[dict]:
    """Re-attach workspaces whose channel was lost between restarts.

    A workspace can become *detached* when its row in ``tasks.md`` lists
    ``-`` as the Thread ID — typically because the agent was created on a
    machine that no longer has access to the channel, or because the topic
    was manually closed and the row reset. On every Telegram boot we walk
    the live workspace list and, for each agent missing a ``thread_id``,
    create a fresh forum topic, persist the new id back to ``tasks.md``,
    and post a welcome message so the channel is immediately usable again.

    Returns the list of workspaces that were healed (each entry has
    ``name``, ``display_name``, ``thread_id``).
    """
    if platform is None:
        return []

    repaired: list[dict] = []
    for agent in manager.list_workspaces():
        if agent.thread_id:
            continue

        thread_id = await platform.create_channel(agent.description)
        if not thread_id:
            log.warning("Failed to heal detached workspace '%s'", agent.name)
            continue

        manager.add_agent(
            name=agent.name,
            work_dir=agent.work_dir,
            description=agent.description,
            agent_type=agent.agent_type,
            model=agent.model,
            thread_id=thread_id,
        )
        _update_queue_entry_thread_id(agent.name, thread_id)

        try:
            await platform.send_to_channel(
                thread_id,
                "*%s* workspace is ready.\nAgent *%s* is assigned to this channel." % (
                    agent.description,
                    agent.name,
                ),
            )
        except Exception as exc:
            log.warning("Welcome message failed for healed workspace '%s': %s", agent.name, exc)

        repaired.append({
            "name": agent.name,
            "display_name": agent.description,
            "thread_id": thread_id,
        })

    if repaired:
        log.info("Healed %d detached workspace(s): %s",
                 len(repaired), ", ".join(r["name"] for r in repaired))
    return repaired
