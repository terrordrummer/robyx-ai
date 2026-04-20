"""0.22.2 → 0.23.0 — unify scheduled/continuous task I/O on parent workspace chat.

Spec 005 retires the dedicated continuous-task sub-topic (`🔄 <name>`) and
routes every continuous-task scheduler report into the parent workspace
chat instead. Pre-0.23.0 continuous tasks persist the *sub-topic*
`thread_id` in `data/continuous/<name>/state.json`; this migration
rewrites it to the parent workspace's `thread_id`, records the legacy
value for audit, materialises a `plan.md` for tasks that never got one
during creation, best-effort closes the legacy sub-topic on the live
platform, and posts a single transition notice in the parent workspace.

Idempotency is enforced by two guards:
  1. Per-state ``migrated_v0_23_0`` timestamp — presence causes a skip on
     every subsequent run for that task.
  2. Process-wide ``data/migrations/v0_23_0.done`` marker — the migration
     runner reads this when deciding whether to invoke ``upgrade`` at all.

Contracts: ``specs/005-unified-workspace-chat/contracts/migration-v0_23_0.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .base import Migration, MigrationContext


DEFAULT_LOG = logging.getLogger("robyx.migrations.v0_23_0")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_data_dir(ctx: MigrationContext) -> Path:
    if ctx.data_dir is not None:
        return Path(ctx.data_dir)
    # Fall back to the real runtime data dir when the runner omitted it.
    from config import DATA_DIR as _DATA_DIR  # type: ignore
    return Path(_DATA_DIR)


def _resolve_parent_thread_id(
    state: dict, manager: Any, log: logging.Logger,
) -> Optional[Any]:
    """Look up the parent workspace's thread_id via the AgentManager.

    Uses ``state.parent_workspace`` (primary), falling back to the legacy
    ``parent_workspace_name`` field some older snapshots carried. Returns
    ``None`` when the manager has no matching agent — the migration skips
    that task and records the name in the summary.
    """
    if manager is None:
        log.warning(
            "migration v0_23_0: no AgentManager available — cannot resolve "
            "parent thread_id for '%s'", state.get("name"),
        )
        return None

    ws_name = state.get("parent_workspace") or state.get("parent_workspace_name")
    if not ws_name:
        log.error(
            "migration v0_23_0: state for '%s' has no parent_workspace field",
            state.get("name"),
        )
        return None

    agent = None
    try:
        agent = manager.get(ws_name)
    except Exception as exc:  # pragma: no cover - defensive
        log.error(
            "migration v0_23_0: manager.get(%r) raised: %s", ws_name, exc,
        )
        return None

    if agent is None:
        log.error(
            "migration v0_23_0: no agent found for parent_workspace=%r "
            "(task '%s' will be skipped)",
            ws_name, state.get("name"),
        )
        return None

    thread_id = getattr(agent, "thread_id", None)
    if thread_id is None:
        log.error(
            "migration v0_23_0: parent_workspace=%r has no thread_id "
            "(task '%s' will be skipped)", ws_name, state.get("name"),
        )
        return None

    return thread_id


def _ensure_plan_md(state: dict, data_dir: Path, log: logging.Logger) -> str:
    """Ensure ``data/continuous/<name>/plan.md`` exists.

    Pre-0.23.0 tasks were created without a dedicated plan artifact. We
    render one from the existing ``program`` dict using the same layout
    new tasks get at creation time, so the primary agent's ``[GET_PLAN]``
    macro and the secondary step agent's prompt context both see a stable
    shape. Returns the relative path (anchored at repo root) to persist
    in state.
    """
    name = state.get("name") or "?"
    program = state.get("program") or {}
    display_name = state.get("display_name") or name

    # Lazy imports so offline/unit runs don't drag in the full stack.
    from continuous import plan_file_path, write_plan_md

    path = plan_file_path(name)
    if not path.exists():
        # Reuse the renderer used at creation time (topics._render_plan_markdown).
        from topics import _render_plan_markdown  # type: ignore
        body = _render_plan_markdown(display_name, program)
        write_plan_md(name, body)
        log.info("migration v0_23_0: generated plan.md for '%s'", name)

    # Compute the relative path from the repo root for portability.
    try:
        repo_root = Path(__file__).resolve().parents[2]
        rel = path.resolve().relative_to(repo_root)
        return str(rel)
    except (ValueError, OSError):
        return str(path)


async def _close_legacy_subtopic(
    platform: Any,
    legacy_thread_id: Any,
    state_name: str,
    log: logging.Logger,
) -> None:
    """Best-effort close of the legacy ``🔄 <name>`` sub-topic.

    Calls ``platform.close_channel(legacy_thread_id)`` — all three
    adapters (Telegram / Discord / Slack) implement it per the Platform
    ABC. On unsupported platforms or transient failures, falls back to a
    final notice posted into the legacy sub-topic so the user sees where
    the task moved. Never raises.
    """
    if platform is None or legacy_thread_id is None:
        return

    closed = False
    try:
        closed = bool(await platform.close_channel(legacy_thread_id))
    except Exception as exc:
        log.warning(
            "migration v0_23_0: close_channel failed for '%s' "
            "(legacy_thread=%r): %s",
            state_name, legacy_thread_id, exc,
        )

    if closed:
        return

    # Fallback: post a final notice in the legacy sub-topic so the user
    # understands the task has moved. Best-effort.
    try:
        await platform.send_to_channel(
            legacy_thread_id,
            "🔄 [%s] migrato nel workspace chat." % state_name,
        )
    except Exception as exc:
        log.warning(
            "migration v0_23_0: fallback notice failed for '%s' "
            "(legacy_thread=%r): %s",
            state_name, legacy_thread_id, exc,
        )


async def _post_transition_notice(
    platform: Any,
    parent_chat_id: Any,
    parent_thread_id: Any,
    state_name: str,
    log: logging.Logger,
) -> None:
    """Post the single "migrato" notice into the parent workspace chat."""
    if platform is None:
        return
    try:
        await platform.send_message(
            chat_id=parent_chat_id,
            text="🔄 [%s] migrato — da ora riporto qui." % state_name,
            thread_id=parent_thread_id,
        )
    except Exception as exc:
        log.warning(
            "migration v0_23_0: transition notice failed for '%s': %s",
            state_name, exc,
        )


def _resolve_parent_chat_id(manager: Any, ws_name: str) -> Any:
    """Return the parent workspace's chat_id if the AgentManager exposes it,
    otherwise ``None`` — ``platform.send_message`` treats ``None`` as
    "use default chat" on most adapters.
    """
    if manager is None or not ws_name:
        return None
    try:
        agent = manager.get(ws_name)
    except Exception:
        return None
    return getattr(agent, "chat_id", None) if agent is not None else None


# ── Migration body ──────────────────────────────────────────────────────────


async def upgrade(ctx: MigrationContext) -> None:
    """Migrate every pre-0.23.0 continuous task to the unified workspace
    chat model. Idempotent and forward-only.
    """
    log = ctx.log or DEFAULT_LOG
    data_dir = _resolve_data_dir(ctx)
    continuous_dir = data_dir / "continuous"
    done_marker = data_dir / "migrations" / "v0_23_0.done"

    # Process-wide short-circuit. Runner also checks its own tracker — this
    # is a second line of defence against re-runs on a partially-recovered
    # state set.
    if done_marker.exists():
        log.info("migration v0_23_0: done marker present — skipping run")
        return

    if not continuous_dir.exists():
        log.info(
            "migration v0_23_0: no data/continuous/ directory — nothing to migrate",
        )
        _write_done_marker(done_marker, log)
        return

    migrated: list[str] = []
    skipped_already: list[str] = []
    skipped_errors: list[tuple[str, str]] = []

    # Import the state helpers lazily so unit tests that stub CONTINUOUS_DIR
    # see the patched value.
    from continuous import load_state, save_state, state_file_path

    for task_dir in sorted(continuous_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        state_path = task_dir / "state.json"
        if not state_path.exists():
            continue

        # Per-task idempotency guard — must come first so partial re-runs
        # never double-migrate a task.
        state = load_state(state_path)
        if state is None:
            skipped_errors.append((task_dir.name, "unreadable state.json"))
            log.error(
                "migration v0_23_0: state.json for '%s' is unreadable — skipping",
                task_dir.name,
            )
            continue
        if state.get("migrated_v0_23_0"):
            skipped_already.append(state.get("name", task_dir.name))
            continue

        name = state.get("name") or task_dir.name

        # Resolve new parent thread_id before touching anything — failures
        # here leave the task untouched for a later retry once the workspace
        # is healed.
        new_thread_id = _resolve_parent_thread_id(state, ctx.manager, log)
        if new_thread_id is None:
            skipped_errors.append((name, "parent workspace unresolved"))
            continue

        legacy_thread_id = state.get("workspace_thread_id")

        # Record the transition in state (atomic write-then-rename via
        # save_state) BEFORE posting notices, so a crash between the write
        # and the send leaves us in a consistent post-migration state.
        state["legacy_workspace_thread_id"] = legacy_thread_id
        state["workspace_thread_id"] = new_thread_id
        try:
            state["plan_path"] = _ensure_plan_md(state, data_dir, log)
        except Exception as exc:
            log.warning(
                "migration v0_23_0: plan.md generation failed for '%s': %s",
                name, exc,
            )
        state["migrated_v0_23_0"] = _now_iso_utc()
        try:
            save_state(state_file_path(name), state)
        except Exception as exc:
            skipped_errors.append((name, "state write failed: %s" % exc))
            log.error(
                "migration v0_23_0: failed to persist new state for '%s': %s",
                name, exc, exc_info=True,
            )
            continue

        # Best-effort platform side effects. Failures here DO NOT unwind
        # the state change — re-running would skip due to the marker,
        # leaving the user without the transition notice. We prefer
        # "one silent migration" over "one unstuck migration that loops".
        if legacy_thread_id not in (None, new_thread_id):
            await _close_legacy_subtopic(
                ctx.platform, legacy_thread_id, name, log,
            )

        parent_chat_id = _resolve_parent_chat_id(
            ctx.manager, state.get("parent_workspace") or "",
        )
        await _post_transition_notice(
            ctx.platform, parent_chat_id, new_thread_id, name, log,
        )

        migrated.append(name)

    log.info(
        "migration v0_23_0: migrated=%d skipped_already=%d skipped_error=%d",
        len(migrated), len(skipped_already), len(skipped_errors),
    )

    if skipped_errors:
        # Do NOT write the done marker and do NOT return normally: the
        # system is partially migrated (some tasks repointed, some still
        # on the legacy sub-topic). Raising halts the chain so the next
        # boot re-enters this migration. Per-task ``migrated_v0_23_0``
        # markers skip the ones that already succeeded; only the failing
        # entries are retried. The operator sees the error in the log and
        # can resolve the underlying issue (missing workspace, disk full,
        # corrupt state.json) before proceeding.
        for nm, reason in skipped_errors:
            log.error(
                "migration v0_23_0: skipped '%s' (%s)", nm, reason,
            )
        failed = ", ".join("%s (%s)" % (nm, reason) for nm, reason in skipped_errors)
        raise RuntimeError(
            "migration v0_23_0: %d task(s) could not be migrated: %s"
            % (len(skipped_errors), failed)
        )

    _write_done_marker(done_marker, log)


def _write_done_marker(path: Path, log: logging.Logger) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_now_iso_utc() + "\n")
    except OSError as exc:
        log.warning(
            "migration v0_23_0: failed to write done marker at %s: %s",
            path, exc,
        )


MIGRATION = Migration(
    from_version="0.22.2",
    to_version="0.23.0",
    description="unify scheduled/continuous task I/O on parent workspace chat",
    upgrade=upgrade,
)
