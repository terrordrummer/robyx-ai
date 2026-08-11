"""Robyx — Dynamic topic/channel management.

Handles creating, closing, and managing channels (Telegram forum topics, etc.)
for workspaces and specialists via the Platform abstraction.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from agents import AgentManager
from config import AGENTS_DIR, SPECIALISTS_DIR, SPECIALISTS_FILE, DATA_DIR
from scheduler import (
    FREQUENCY_SECONDS,
    add_task as _add_task,
    cancel_tasks_for_agent_file as _cancel_tasks_for_agent_file,
    validate_one_shot_scheduled_at as _validate_one_shot_scheduled_at,
)
from task_scope import TaskScope, attach_scope

log = logging.getLogger("robyx.topics")

# Reserved names that must never be used for a workspace or specialist:
# - ``robyx`` / ``orchestrator`` would overwrite the Principal Orchestrator
#   entry in AgentManager and brick the bot.
# - The empty string is what ``_sanitize_task_name`` returns for inputs
#   made entirely of punctuation — we must refuse those so we never write
#   ``data/agents/.md`` or register a nameless agent.
RESERVED_AGENT_NAMES = frozenset({"robyx", "orchestrator", ""})


def _sanitize_task_name(name: str) -> str:
    """Convert a display name to a safe task/file name.

    The mapping is **not injective**: case-insensitive, and every run of
    non-alphanumeric characters collapses to a single ``-``. So
    ``"My-Project!"``, ``"my project"``, and ``"MY_PROJECT"`` all fold to
    ``"my-project"``. ``_validate_new_agent_name`` catches the resulting
    collision before any side-effect runs (manager lookup, file write,
    topic creation), so the duplicate surfaces as a user-visible
    "name already in use" error rather than silent overwrite.
    """
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


def _normalize_backend_choice(
    backend: str | None,
    *,
    kind: str,
    display_name: str,
) -> str | None:
    """Validate the optional per-agent backend override.

    Returns ``None`` for empty / placeholder values (so the agent inherits
    the global default), or the lowercased backend key for explicit
    overrides. Unknown keys raise ``ValueError`` BEFORE any side effect:
    callers create the channel / write the agent file / queue the task
    *after* this validation, so a bad name never leaves debris.
    """
    if backend is None:
        return None
    value = str(backend).strip().lower()
    if value in ("", "default", "none"):
        return None
    # Imported lazily so a future refactor of ai_backend cannot turn this
    # into an import cycle (topics is imported very early by handlers).
    from ai_backend import list_backends
    supported = list_backends()
    if value not in supported:
        raise ValueError(
            "cannot create %s '%s': unknown backend '%s' (supported: %s)"
            % (kind, display_name, backend, ", ".join(sorted(supported)))
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
    backend: str | None = None,
    workspace_scope: TaskScope | None = None,
) -> dict | None:
    """Full workspace creation: channel + agent file + tasks.md entry + agent registration.

    ``backend`` is the optional per-workspace AI backend override. When set
    (``"claude"`` / ``"codex"`` / ``"opencode"``) the workspace runs on that
    CLI regardless of the global ``AI_BACKEND``. Unknown values are
    rejected with ``ValueError`` BEFORE any side effect (channel, files,
    queue entry) so a typo never leaves a half-created workspace behind.

    Returns dict with workspace info or None on failure.
    """
    display_name = _validate_table_safe_display_name(name, "workspace")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "workspace")
    backend = _normalize_backend_choice(backend, kind="workspace", display_name=display_name)
    normalized_scheduled_at = scheduled_at
    if task_type == "one-shot":
        normalized_scheduled_at = _validate_one_shot_scheduled_at(
            scheduled_at,
            label="one-shot workspaces",
        )
    if task_type in {"one-shot", "scheduled"} and workspace_scope is None:
        raise ValueError(
            "workspace scope is required for scheduled workspace creation"
        )

    # 1. Create channel/topic
    if platform is None:
        log.error("Cannot create workspace '%s': no platform available", name)
        return None
    thread_id = await platform.create_channel(display_name)
    if not thread_id:
        return None
    child_scope = (
        workspace_scope.for_parent_channel(thread_id)
        if workspace_scope is not None
        else None
    )

    # 2. Write agent instructions file
    agent_file = AGENTS_DIR / ("%s.md" % safe_name)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Inject config into instructions
    full_instructions = "# %s\n\n%s\n" % (display_name, instructions.strip())
    try:
        agent_file.write_text(full_instructions)
    except OSError as exc:
        log.error("Failed to write agent file %s: %s", agent_file, exc)
        return None
    log.info("Wrote agent instructions: %s", agent_file)

    # 3. Register the task in the unified queue
    if task_type == "one-shot":
        entry = {
            "name": safe_name,
            "agent_file": "agents/%s.md" % safe_name,
            "prompt": "",
            "type": "one-shot",
            "scheduled_at": normalized_scheduled_at,
            "model": model,
            "thread_id": str(thread_id),
            "description": display_name,
        }
        if backend:
            entry["backend"] = backend
        if child_scope is not None:
            attach_scope(entry, child_scope)
        _add_task(entry, scope=child_scope)
    elif task_type == "scheduled":
        freq_str = frequency if frequency != "none" else "hourly"
        interval = FREQUENCY_SECONDS.get(freq_str, 3600)
        from datetime import datetime, timezone
        entry = {
            "name": safe_name,
            "agent_file": "agents/%s.md" % safe_name,
            "type": "periodic",
            "interval_seconds": interval,
            "next_run": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "thread_id": str(thread_id),
            "description": display_name,
        }
        if backend:
            entry["backend"] = backend
        if child_scope is not None:
            attach_scope(entry, child_scope)
        _add_task(entry, scope=child_scope)
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
        backend=backend,
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
    """Close a workspace after draining all of its continuous children."""
    agent = manager.get(name)
    if not agent:
        return False

    # Prevent ordinary scheduled work from starting while continuous children
    # are draining.  Continuous entries use their own agent file and are
    # canceled by ``drain_and_cancel_continuous_task`` below.
    agent_file = "agents/%s.md" % agent.name
    close_transaction_id = uuid.uuid4().hex
    canceled = _cancel_tasks_for_agent_file(
        agent_file,
        reason="workspace closed",
        transaction_id=close_transaction_id,
    )

    from continuous import CONTINUOUS_DIR, load_state, pause_task, save_state
    from lifecycle_macros import _lifecycle_task_lock
    from scheduler import (
        drain_and_cancel_continuous_task,
        restore_tasks_canceled_by_transaction,
    )
    partially_closed_children: set[str] = set()

    async def _drain_child(state_path, task_name: str) -> None:
        # Capture the generation under the lifecycle lock, but never hold the
        # lock while awaiting the process/delivery watcher: successful
        # delivery may acquire the same lock in mark_topic_reachable.
        async with _lifecycle_task_lock(task_name):
            before = load_state(state_path)
            if before is None:
                raise RuntimeError("continuous child '%s' state disappeared" % task_name)
            generation = (
                before.get("created_at"),
                before.get("name"),
                before.get("parent_workspace"),
                before.get("branch"),
            )

        # Once drain begins its queue cancellation/process stop cannot be
        # safely undone. On a later sibling failure this child remains in a
        # coherent stopped+canceled partial-close state.
        partially_closed_children.add(task_name)
        try:
            outcome = await drain_and_cancel_continuous_task(
                task_name,
                reason="workspace closed",
            )
        except BaseException as exc:
            error_text = str(exc)[:240]

            async def _record_partial_close() -> None:
                async with _lifecycle_task_lock(task_name):
                    failed = load_state(state_path)
                    if failed is not None:
                        failed["workspace_close_error"] = error_text
                        failed["workspace_close_partial"] = True
                        save_state(state_path, failed)

            await _await_cleanup_uninterruptibly(_record_partial_close())
            raise
        if outcome.get("process_found") and not outcome.get("tree_stopped"):
            raise RuntimeError(
                "continuous child '%s' process tree did not drain" % task_name,
            )

        async with _lifecycle_task_lock(task_name):
            # Reload after the child and its delivery watcher have drained: a
            # graceful SIGTERM handler may have persisted one final snapshot.
            fresh = load_state(state_path)
            if fresh is None:
                raise RuntimeError("continuous child '%s' state disappeared" % task_name)
            current_generation = (
                fresh.get("created_at"),
                fresh.get("name"),
                fresh.get("parent_workspace"),
                fresh.get("branch"),
            )
            if current_generation != generation:
                raise RuntimeError(
                    "continuous child '%s' generation changed during close" % task_name,
                )
            if fresh.get("status") in ("completed", "deleted"):
                return
            pause_task(fresh)
            fresh.pop("delivery_state_override", None)
            fresh.pop("workspace_close_error", None)
            fresh.pop("workspace_close_partial", None)
            save_state(state_path, fresh)

    try:
        children = []
        continuous_root = CONTINUOUS_DIR
        if continuous_root.exists():
            for task_dir in sorted(continuous_root.iterdir()):
                state_path = task_dir / "state.json"
                if not state_path.exists():
                    continue
                try:
                    state = load_state(state_path)
                except Exception as exc:
                    log.error(
                        "Cannot inspect continuous child %s while closing '%s': %s",
                        task_dir.name,
                        agent.name,
                        exc,
                    )
                    raise RuntimeError(
                        "cannot safely close workspace '%s': child state '%s' unavailable"
                        % (agent.name, task_dir.name)
                    ) from exc
                if (
                    state
                    and state.get("parent_workspace") == agent.name
                    and state.get("status") not in ("completed", "deleted", "stopped")
                ):
                    children.append(
                        _drain_child(state_path, state.get("name") or task_dir.name)
                    )
        if children:
            await asyncio.gather(*children)

        if agent.thread_id and platform is not None:
            await platform.send_to_channel(
                agent.thread_id,
                "Workspace *%s* closed." % name,
            )
            if not await platform.close_channel(agent.thread_id):
                raise RuntimeError("platform refused to close workspace '%s'" % name)
    except BaseException:
        restore_tasks_canceled_by_transaction(
            agent_file,
            close_transaction_id,
            exclude_names=partially_closed_children,
        )
        raise

    if canceled:
        log.info(
            "Closed workspace '%s' and canceled %d pending task(s)",
            agent.name,
            canceled,
        )

    # Remove from agent manager
    manager.remove_agent(name)
    return True


async def _await_cleanup_uninterruptibly(awaitable):
    """Finish mandatory compensation despite repeated caller cancellation."""
    cleanup = asyncio.create_task(awaitable)
    while True:
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue


async def _run_supervised_command(
    command: list[str],
    *,
    timeout: float,
    owner: str,
):
    """Run a short mutation child isolated, tracked, and cancellation-safe."""
    import subprocess
    import sys
    from runtime_supervisor import get_runtime_supervisor

    supervisor = get_runtime_supervisor()
    supervisor.reject_if_closing()
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    try:
        supervisor.track_process(
            proc,
            owner=owner,
            process_group=sys.platform != "win32",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if supervisor.process_tree_alive(proc):
            stopped = await supervisor.terminate_process(proc, grace_seconds=1.0)
            if not stopped:
                raise RuntimeError("command process tree did not stop: %s" % command[0])
        else:
            supervisor.untrack_process(proc)
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    except BaseException:
        if supervisor.process_tree_alive(proc):
            await _await_cleanup_uninterruptibly(
                supervisor.terminate_process(proc, grace_seconds=1.0),
            )
        raise


def _git_dir_signature(git_dir: Path) -> tuple[tuple[str, bytes], ...] | None:
    """Snapshot a freshly-created git metadata tree for safe compensation."""
    try:
        items: list[tuple[str, bytes]] = []
        for path in sorted(git_dir.rglob("*")):
            if path.is_file():
                items.append((str(path.relative_to(git_dir)), path.read_bytes()))
        return tuple(items)
    except OSError:
        return None


async def _run_git_for_compensation(work_dir: str, *args: str) -> tuple[int, str]:
    result = await _run_supervised_command(
        ["git", "-C", work_dir, *args],
        timeout=10.0,
        owner="continuous-git-rollback",
    )
    return result.returncode, result.stdout.decode(errors="replace").strip()


async def _compensate_git_setup(work_dir: str, git_info: dict) -> bool:
    """Undo only git state proven to have been created by this transaction."""
    import shutil

    git_dir = Path(work_dir) / ".git"
    if git_info.get("created_repo"):
        expected = git_info.get("git_dir_signature")
        if expected is not None and _git_dir_signature(git_dir) == expected:
            shutil.rmtree(git_dir)
            return True
        log.error(
            "Creation rollback left git repo at %s: metadata changed after setup; "
            "operator review required",
            git_dir,
        )
        return False

    if not (git_info.get("created_branch") or git_info.get("switched_existing")):
        return True
    branch = str(git_info.get("branch") or "")
    previous = str(git_info.get("previous_branch") or "")
    _, current = await _run_git_for_compensation(work_dir, "branch", "--show-current")
    _, head = await _run_git_for_compensation(work_dir, "rev-parse", "HEAD")
    _, status = await _run_git_for_compensation(work_dir, "status", "--porcelain")
    if (
        current != branch
        or head != str(git_info.get("head_after") or "")
        or status != str(git_info.get("status_after") or "")
        or not previous
    ):
        log.error(
            "Creation rollback left git branch '%s' in %s: repository changed "
            "after setup; operator review required",
            branch,
            work_dir,
        )
        return False
    checkout_rc, _ = await _run_git_for_compensation(work_dir, "checkout", previous)
    if checkout_rc != 0:
        log.error("Creation rollback could not restore git branch '%s'", previous)
        return False
    if git_info.get("created_branch"):
        delete_rc, _ = await _run_git_for_compensation(work_dir, "branch", "-D", branch)
        if delete_rc != 0:
            log.error("Creation rollback could not delete created branch '%s'", branch)
            return False
    return True


async def _setup_git_branch(work_dir: str, branch: str) -> dict:
    """Set up a git branch for continuous task work in the target project.

    Returns a dict with:
      - ``branch``: the actual branch name (may differ if user's repo uses it)
      - ``versioning``: ``"git-branch"`` | ``"git-init"`` | ``"none"``
      - ``message``: human-readable description of what was done

    Three scenarios:
    1. work_dir is already a git repo → create branch there
    2. work_dir is not a git repo → git init + create branch
    3. git is not available → proceed without versioning
    """
    import subprocess
    from pathlib import Path

    async def _run_git(*args, timeout=10):
        try:
            return await _run_supervised_command(
                ["git", *args],
                timeout=float(timeout),
                owner="continuous-git-setup",
            )
        except asyncio.TimeoutError as exc:
            raise subprocess.TimeoutExpired(["git", *args], timeout) from exc

    work_path = Path(work_dir)

    # Check if git is available
    try:
        await _run_git("--version", timeout=5)
    except (FileNotFoundError, Exception):
        return {
            "branch": branch,
            "versioning": "none",
            "message": "git not available — proceeding without versioning",
        }

    # Check if work_dir is already a git repo
    is_repo = (work_path / ".git").exists()
    if not is_repo:
        try:
            result = await _run_git("-C", work_dir, "rev-parse", "--git-dir", timeout=5)
            is_repo = result.returncode == 0
        except Exception:
            pass

    if is_repo:
        previous_result = await _run_git(
            "-C", work_dir, "branch", "--show-current", timeout=5,
        )
        previous_branch = previous_result.stdout.decode(errors="replace").strip()
        head_before = await _run_git("-C", work_dir, "rev-parse", "HEAD", timeout=5)
        status_before = await _run_git(
            "-C", work_dir, "status", "--porcelain", timeout=5,
        )
        # Create branch in existing repo
        try:
            result = await _run_git("-C", work_dir, "checkout", "-b", branch)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr,
                )
            head_after = await _run_git("-C", work_dir, "rev-parse", "HEAD", timeout=5)
            status_after = await _run_git("-C", work_dir, "status", "--porcelain", timeout=5)
            return {
                "branch": branch,
                "versioning": "git-branch",
                "message": "created branch `%s` in existing repo" % branch,
                "created_branch": True,
                "previous_branch": previous_branch,
                "head_after": head_after.stdout.decode(errors="replace").strip(),
                "status_after": status_after.stdout.decode(errors="replace").strip(),
            }
        except subprocess.CalledProcessError as exc:
            if b"already exists" in (exc.stderr or b""):
                try:
                    result = await _run_git("-C", work_dir, "checkout", branch)
                    if result.returncode == 0:
                        head_after = await _run_git(
                            "-C", work_dir, "rev-parse", "HEAD", timeout=5,
                        )
                        status_after = await _run_git(
                            "-C", work_dir, "status", "--porcelain", timeout=5,
                        )
                        return {
                            "branch": branch,
                            "versioning": "git-branch",
                            "message": "switched to existing branch `%s`" % branch,
                            "switched_existing": branch != previous_branch,
                            "previous_branch": previous_branch,
                            "head_after": head_after.stdout.decode(errors="replace").strip(),
                            "status_after": status_after.stdout.decode(errors="replace").strip(),
                        }
                except Exception:
                    pass
            log.warning("Failed to create branch '%s' in %s: %s", branch, work_dir, exc.stderr)
            return {
                "branch": branch,
                "versioning": "none",
                "message": "branch creation failed — proceeding without versioning",
            }
        except BaseException:
            claim = {
                "branch": branch,
                "created_branch": True,
                "previous_branch": previous_branch,
                "head_after": head_before.stdout.decode(errors="replace").strip(),
                "status_after": status_before.stdout.decode(errors="replace").strip(),
            }
            await _await_cleanup_uninterruptibly(
                _compensate_git_setup(work_dir, claim),
            )
            raise
    else:
        # Initialize a new repo
        try:
            result = await _run_git("-C", work_dir, "init")
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr,
                )
            result = await _run_git("-C", work_dir, "checkout", "-b", branch)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr,
                )
            return {
                "branch": branch,
                "versioning": "git-init",
                "message": "initialized git repo and created branch `%s`" % branch,
                "created_repo": True,
                "git_dir_signature": _git_dir_signature(work_path / ".git"),
            }
        except Exception as exc:
            log.warning("Failed to init git in %s: %s", work_dir, exc)
            return {
                "branch": branch,
                "versioning": "none",
                "message": "git init failed — proceeding without versioning",
            }
        except BaseException:
            if (work_path / ".git").exists():
                claim = {
                    "created_repo": True,
                    "git_dir_signature": _git_dir_signature(work_path / ".git"),
                }
                await _await_cleanup_uninterruptibly(
                    _compensate_git_setup(work_dir, claim),
                )
            raise


@dataclass
class _ContinuousCreationReservation:
    dedicated_thread_id: object | None = None
    git_info: dict | None = None
    git_work_dir: str | None = None


async def create_continuous_workspace(
    name: str,
    program: dict,
    work_dir: str,
    parent_workspace: str,
    model: str,
    manager: AgentManager,
    platform=None,
    parent_thread_id=None,
    workspace_scope: TaskScope | None = None,
    drain_timeout_seconds: int | None = None,
) -> dict | None:
    """Serialize same-name creation and compensate partial side effects.

    Name validation alone is insufficient because git/topic creation awaits:
    two requests can both validate before either registers the agent.  The
    lifecycle lock is the shared per-name transaction boundary used by
    create/stop/complete/delete/recovery.
    """
    from continuous import state_file_path
    from lifecycle_macros import _lifecycle_task_lock
    import scheduler as scheduler_mod

    display_name = _validate_table_safe_display_name(name, "continuous workspace")
    safe_name = _sanitize_task_name(display_name)
    if parent_thread_id is None:
        log.error(
            "create_continuous_workspace '%s' called without parent_thread_id",
            safe_name,
        )
        return None
    if workspace_scope is None:
        raise ValueError(
            "workspace scope is required for continuous workspace creation"
        )
    state_path = state_file_path(safe_name)
    plan_path = state_path.parent / "plan.md"
    program_path = state_path.parent / "program.json"
    agent_path = AGENTS_DIR / ("%s.md" % safe_name)

    def _snapshot(path: Path) -> bytes | None:
        return path.read_bytes() if path.exists() else None

    def _restore(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    async with _lifecycle_task_lock(safe_name):
        previous_files = {
            state_path: _snapshot(state_path),
            plan_path: _snapshot(plan_path),
            program_path: _snapshot(program_path),
            agent_path: _snapshot(agent_path),
        }
        previous_entries = [
            dict(entry)
            for entry in scheduler_mod.load_queue()
            if entry.get("type") == "continuous" and entry.get("name") == safe_name
        ]
        previous_agent = manager.get(safe_name)
        reservation = _ContinuousCreationReservation()
        try:
            return await _create_continuous_workspace_reserved(
                name=name,
                program=program,
                work_dir=work_dir,
                parent_workspace=parent_workspace,
                model=model,
                manager=manager,
                platform=platform,
                parent_thread_id=parent_thread_id,
                workspace_scope=workspace_scope,
                drain_timeout_seconds=drain_timeout_seconds,
                _reservation=reservation,
            )
        except BaseException:
            # Restore just this name's queue slice; unrelated concurrent queue
            # updates remain untouched under the scheduler's process mutex.
            scheduler_mod.rollback_continuous_creation(
                safe_name,
                previous_entries,
            )
            current_agent = manager.get(safe_name)
            if current_agent is not None and current_agent is not previous_agent:
                try:
                    manager.remove_agent(safe_name)
                except Exception:
                    log.error("Creation rollback could not unregister '%s'", safe_name)
            for path, content in previous_files.items():
                try:
                    _restore(path, content)
                except OSError:
                    log.error("Creation rollback could not restore %s", path, exc_info=True)
            if reservation.dedicated_thread_id is not None and platform is not None:
                try:
                    if hasattr(platform, "archive_topic"):
                        await _await_cleanup_uninterruptibly(
                            platform.archive_topic(
                                reservation.dedicated_thread_id,
                                display_name,
                            ),
                        )
                    elif hasattr(platform, "close_channel"):
                        await _await_cleanup_uninterruptibly(
                            platform.close_channel(reservation.dedicated_thread_id),
                        )
                except Exception:
                    log.error(
                        "Creation rollback could not archive topic %s",
                        reservation.dedicated_thread_id,
                        exc_info=True,
                    )
            if reservation.git_info is not None and reservation.git_work_dir is not None:
                await _await_cleanup_uninterruptibly(
                    _compensate_git_setup(
                        reservation.git_work_dir,
                        reservation.git_info,
                    ),
                )
            raise


async def _create_continuous_workspace_reserved(
    name: str,
    program: dict,
    work_dir: str,
    parent_workspace: str,
    model: str,
    manager: AgentManager,
    platform=None,
    parent_thread_id=None,
    workspace_scope: TaskScope | None = None,
    drain_timeout_seconds: int | None = None,
    _reservation: _ContinuousCreationReservation | None = None,
) -> dict | None:
    """Create a continuous task: git branch + state + plan.md + queue entry
    + dedicated topic (spec 006 US2).

    Spec 006 supersedes spec 005's unified-chat model for continuous tasks:
    each task gets a **dedicated topic** named ``[Continuous] <display_name>``
    with a state-marker suffix (``· ▶`` on creation). All subsequent step
    deliveries, awaiting-input pins, state transitions, and the
    ``[GET_EVENTS]`` fallbacks target this dedicated thread — the parent
    workspace topic stays clean for human↔agent conversation.

    New tasks fail closed if their dedicated topic cannot be created. Legacy
    migration is the only path allowed to retain parent-thread routing.

    Returns dict with workspace info or None on failure.
    """
    from continuous import (
        create_continuous_task,
        state_file_path,
        write_plan_md,
    )

    display_name = _validate_table_safe_display_name(name, "continuous workspace")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "continuous workspace")

    if parent_thread_id is None:
        log.error(
            "create_continuous_workspace '%s' called without parent_thread_id",
            safe_name,
        )
        return None

    branch = "continuous/%s" % safe_name

    # 1. Set up git branch in the target project's work_dir
    git_info = await _setup_git_branch(work_dir, branch)
    if _reservation is not None:
        _reservation.git_info = git_info
        _reservation.git_work_dir = work_dir
    branch = git_info["branch"]
    versioning = git_info["versioning"]
    log.info(
        "Continuous '%s' git setup: %s (%s)",
        safe_name, git_info["message"], versioning,
    )

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

    # 3. Persist the per-task plan.md (spec 005). Readable by the primary
    # agent on demand via [GET_PLAN] and by the secondary step agent in its
    # prompt context.
    plan_md = _render_plan_markdown(display_name, program)
    plan_path = write_plan_md(safe_name, plan_md)

    # 4. Spec 006 — create the dedicated topic BEFORE writing state, so
    # ``dedicated_thread_id`` can be persisted atomically with the rest of the
    # initial state. Parent-topic fallback would violate task isolation.
    if platform is None or not hasattr(platform, "create_channel"):
        raise RuntimeError("dedicated topic creation is unavailable for '%s'" % safe_name)
    from continuous_state_machine import marker_suffix
    base_title = "[Continuous] %s" % display_name
    try:
        dedicated_thread_id = await platform.create_channel(base_title)
    except Exception as exc:
        raise RuntimeError(
            "dedicated topic creation failed for '%s': %s" % (safe_name, exc)
        ) from exc
    if dedicated_thread_id is None:
        raise RuntimeError("dedicated topic creation failed for '%s'" % safe_name)
    if _reservation is not None:
        _reservation.dedicated_thread_id = dedicated_thread_id
    suffix = marker_suffix("pending")
    if suffix and hasattr(platform, "edit_topic_title"):
        try:
            await platform.edit_topic_title(dedicated_thread_id, base_title + suffix)
        except Exception as exc:
            log.warning("Could not apply initial marker for '%s': %s", safe_name, exc)

    # 5. Create state file with the dedicated_thread_id persisted from the start.
    state = create_continuous_task(
        name=safe_name,
        parent_workspace=parent_workspace,
        program=program,
        thread_id=parent_thread_id,
        branch=branch,
        work_dir=work_dir,
        workspace_scope=workspace_scope,
        plan_text=plan_md,
    )
    state["versioning"] = versioning
    state["dedicated_thread_id"] = dedicated_thread_id
    if drain_timeout_seconds is not None:
        state["drain_timeout_seconds"] = int(drain_timeout_seconds)
    # Relative path from repo root for portability across machines (spec 005).
    from pathlib import Path as _Path
    repo_root = _Path(__file__).resolve().parents[1]
    try:
        state["plan_path"] = str(plan_path.resolve().relative_to(repo_root))
    except ValueError:
        state["plan_path"] = str(plan_path)
    from continuous import save_state, state_file_path as _sfp
    save_state(_sfp(safe_name), state)

    # 6. Create data directory
    (DATA_DIR / safe_name).mkdir(parents=True, exist_ok=True)

    # 7. Add to unified queue. Delivery target is the dedicated topic
    # New tasks always route to their dedicated topic.
    queue_thread_id = dedicated_thread_id
    queue_entry = {
        "name": safe_name,
        "type": "continuous",
        "agent_file": "agents/%s.md" % safe_name,
        "model": model,
        "thread_id": str(queue_thread_id),
        "state_file": str(state_file_path(safe_name)),
        "description": "Continuous: %s" % display_name,
    }
    if workspace_scope is not None:
        attach_scope(queue_entry, workspace_scope)
    _add_task(queue_entry, scope=workspace_scope)

    # 8. Register agent. thread_id points at the dedicated topic when
    # present (so the agent can be routed to by thread lookup); falls
    # back to None (non-hijacking) when no dedicated topic exists.
    manager.add_agent(
        name=safe_name,
        work_dir=work_dir,
        description="[Continuous] %s" % display_name,
        agent_type="workspace",
        model=model,
        thread_id=dedicated_thread_id,
    )

    # 9. Spec 006 — journal the creation event for pull-based queries.
    try:
        import events as events_mod
        events_mod.append(
            task_name=safe_name,
            task_type="continuous",
            event_type="created",
            outcome="ok",
            payload={
                "dedicated_thread_id": dedicated_thread_id,
                "parent_thread_id": parent_thread_id,
                "drain_timeout_seconds": state.get("drain_timeout_seconds", 3600),
            },
        )
    except Exception:
        pass

    return {
        "name": safe_name,
        "display_name": display_name,
        "thread_id": dedicated_thread_id,
        "dedicated_thread_id": dedicated_thread_id,
        "parent_thread_id": parent_thread_id,
        "branch": branch,
        "versioning": versioning,
        "state_file": str(state_file_path(safe_name)),
        "plan_path": str(plan_path),
        "type": "continuous",
    }


def _render_plan_markdown(display_name: str, program: dict) -> str:
    """Render a continuous-task plan.md body from the program payload.

    The output is the authoritative per-task plan consulted by the primary
    agent (via [GET_PLAN]) and by the secondary step agent's prompt
    template. Structure matches ``data-model.md``.
    """
    def _section(title: str, body: str) -> str:
        return "## %s\n%s\n" % (title, body.rstrip() if body else "_n/a_")

    def _bullets(items) -> str:
        if not items:
            return "_n/a_"
        out = []
        for item in items:
            out.append("- %s" % str(item).strip())
        return "\n".join(out)

    objective = program.get("objective") or ""
    success = program.get("success_criteria") or []
    constraints = program.get("constraints") or []
    checkpoint = program.get("checkpoint_policy") or "on-demand"
    context = program.get("context") or ""
    first_step = program.get("first_step") or {}
    first_step_desc = ""
    if isinstance(first_step, dict):
        first_step_desc = first_step.get("description") or ""
    elif isinstance(first_step, str):
        first_step_desc = first_step

    parts: list[str] = [
        "# Plan: %s\n" % display_name,
        _section("Objective", objective),
        _section("Success criteria", _bullets(success)),
        _section("Constraints", _bullets(constraints)),
        _section("Checkpoint policy", checkpoint),
        _section("First step", first_step_desc or "_n/a_"),
    ]
    if context:
        parts.append(_section("Context", context))
    return "\n".join(parts).rstrip() + "\n"


async def create_specialist(
    name: str,
    model: str,
    instructions: str,
    manager: AgentManager,
    work_dir: str,
    platform=None,
    backend: str | None = None,
) -> dict | None:
    """Create a cross-functional specialist agent.

    ``backend`` is the optional per-specialist AI backend override
    (``"claude"`` / ``"codex"`` / ``"opencode"``); ``None`` keeps the
    global default. Unknown values raise ``ValueError`` before any side
    effect.
    """
    display_name = _validate_table_safe_display_name(name, "specialist")
    safe_name = _sanitize_task_name(display_name)
    _validate_new_agent_name(safe_name, manager, "specialist")
    backend = _normalize_backend_choice(backend, kind="specialist", display_name=display_name)

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
        backend=backend,
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
