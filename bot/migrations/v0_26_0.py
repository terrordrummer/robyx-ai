"""0.25.1 → 0.26.0 — continuous-task observability & lifecycle robustness.

Spec 006 gives every continuous task its own dedicated messaging-platform
topic, introduces an append-only event journal at ``data/events.jsonl``,
formalises the lifecycle state machine (running / awaiting_input /
rate_limited / stopped / completed / error / deleted), and adds per-task
configurable drain timeouts on workspace-close.

This migration:

1. Initialises the event journal directory (``data/events/``) and hot file
   (``data/events.jsonl``) so subsequent appends do not race against
   directory creation.
2. For each existing continuous task whose ``dedicated_thread_id`` is not
   yet set and whose status is not ``deleted``, creates a dedicated topic
   ``[Continuous] <display_name>`` via the platform adapter, updates the
   state (``dedicated_thread_id``, ``drain_timeout_seconds`` default,
   ``migrated_v0_26_0`` timestamp), re-points the queue entry's
   ``thread_id`` to the new topic, and seeds a ``migration`` event in the
   journal with both old and new thread ids.
3. Sets the dedicated topic's title to include the current state marker
   suffix (``· ▶`` / ``· ⏸`` / ``· ⏳`` / ``· ⏹`` / ``· ✅`` / ``· ❌``).
4. For tasks already in ``awaiting_input`` with a stored question,
   retroactively posts and pins the awaiting-input message into the new
   dedicated topic so users land on the correct state on first boot.

Idempotency is enforced by two guards:

1. Per-state ``migrated_v0_26_0`` timestamp — presence causes a skip on
   every subsequent run for that task (same pattern as v0_23_0).
2. Process-wide ``data/migrations/v0_26_0.done`` marker — the migration
   runner reads this when deciding whether to invoke ``upgrade`` at all.

A partial failure (e.g. platform rate-limit mid-run) leaves per-task
state atomically consistent; the ``upgrade`` function raises so the
runner halts the chain and the next boot re-enters this migration to
complete the remainder.

Contracts: ``specs/006-continuous-task-robustness/data-model.md`` §8.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .base import Migration, MigrationContext


DEFAULT_LOG = logging.getLogger("robyx.migrations.v0_26_0")


_STATE_MARKER_SUFFIX = {
    "created": " · ▶",
    "running": " · ▶",
    "awaiting_input": " · ⏸",
    "awaiting-input": " · ⏸",  # legacy form tolerated on read
    "rate_limited": " · ⏳",
    "rate-limited": " · ⏳",  # legacy form
    "stopped": " · ⏹",
    "paused": " · ⏹",  # legacy alias
    "completed": " · ✅",
    "error": " · ❌",
}


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_data_dir(ctx: MigrationContext) -> Path:
    if ctx.data_dir is not None:
        return Path(ctx.data_dir)
    from config import DATA_DIR as _DATA_DIR  # type: ignore
    return Path(_DATA_DIR)


def _seed_journal_entry(
    journal_path: Path,
    task_name: str,
    payload: dict,
    log: logging.Logger,
) -> None:
    """Append a single ``migration`` event to the journal.

    Kept deliberately independent of ``bot.events`` so the migration can
    run before that module is imported for the first time (e.g. on fresh
    installs where the scheduler has not yet touched the journal).
    """
    entry = {
        "ts": _now_iso_utc(),
        "task_name": task_name,
        "task_type": "continuous",
        "event_type": "migration",
        "outcome": "ok",
        "payload": payload,
    }
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:
        log.warning(
            "migration v0_26_0: failed to seed journal entry for '%s': %s",
            task_name, exc,
        )


def _write_done_marker(path: Path, log: logging.Logger) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_now_iso_utc() + "\n")
    except OSError as exc:
        log.warning(
            "migration v0_26_0: failed to write done marker at %s: %s",
            path, exc,
        )


def _resolve_parent_chat_id(manager: Any, ws_name: str) -> Optional[Any]:
    if not manager or not ws_name:
        return None
    try:
        agent = manager.get(ws_name)
    except Exception:  # pragma: no cover - defensive
        return None
    return getattr(agent, "chat_id", None) if agent is not None else None


async def _create_dedicated_topic(
    platform: Any,
    display_name: str,
    status: str,
    log: logging.Logger,
) -> Optional[int]:
    """Create the ``[Continuous] <display_name>`` topic and apply the
    state marker suffix to its title. Returns the new thread_id, or None
    on platform failure.
    """
    if platform is None:
        return None

    base_title = "[Continuous] %s" % display_name
    try:
        thread_id = await platform.create_channel(base_title)
    except Exception as exc:  # pragma: no cover - defensive
        log.error(
            "migration v0_26_0: create_channel failed for '%s': %s",
            display_name, exc,
        )
        return None

    if thread_id is None:
        return None

    suffix = _STATE_MARKER_SUFFIX.get(status, " · ▶")
    final_title = base_title + suffix
    if suffix:
        try:
            await platform.edit_topic_title(thread_id, final_title)
        except Exception as exc:
            log.warning(
                "migration v0_26_0: edit_topic_title failed for '%s' (%s): %s",
                display_name, thread_id, exc,
            )
    return thread_id


async def upgrade(ctx: MigrationContext) -> None:
    """Migrate every pre-0.26.0 continuous task to the dedicated-topic model.

    Idempotent and forward-only. See module docstring for full semantics.
    """
    log = ctx.log or DEFAULT_LOG
    data_dir = _resolve_data_dir(ctx)
    continuous_dir = data_dir / "continuous"
    events_dir = data_dir / "events"
    journal_path = data_dir / "events.jsonl"
    done_marker = data_dir / "migrations" / "v0_26_0.done"

    if done_marker.exists():
        log.info("migration v0_26_0: done marker present — skipping run")
        return

    # Step 1 — initialise journal infrastructure (idempotent).
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
        if not journal_path.exists():
            journal_path.touch()
    except OSError as exc:
        log.warning(
            "migration v0_26_0: failed to initialise events dir/file: %s", exc,
        )

    if not continuous_dir.exists():
        log.info(
            "migration v0_26_0: no data/continuous/ directory — no tasks to migrate",
        )
        _write_done_marker(done_marker, log)
        return

    migrated: list[str] = []
    skipped_already: list[str] = []
    skipped_errors: list[tuple[str, str]] = []

    # Import lazily so tests that stub CONTINUOUS_DIR see the patched value.
    from continuous import load_state, save_state, state_file_path

    # The topic-creation side effects require the US2 implementation. If
    # the platform adapter lacks ``create_channel`` or the new ABC methods
    # (e.g. running this migration against an older adapter build), the
    # migration still performs step 1 and records a no-op per task so
    # ``migrated_v0_26_0`` is set and future boots do not re-enter.
    platform_ready = (
        ctx.platform is not None
        and hasattr(ctx.platform, "create_channel")
        and hasattr(ctx.platform, "edit_topic_title")
    )

    for task_dir in sorted(continuous_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        state_path = task_dir / "state.json"
        if not state_path.exists():
            continue

        state = load_state(state_path)
        if state is None:
            skipped_errors.append((task_dir.name, "unreadable state.json"))
            log.error(
                "migration v0_26_0: state.json for '%s' is unreadable — skipping",
                task_dir.name,
            )
            continue
        if state.get("migrated_v0_26_0"):
            skipped_already.append(state.get("name", task_dir.name))
            continue

        name = state.get("name") or task_dir.name
        status = str(state.get("status", "running"))

        # Skip terminal-deleted tasks — they have no dedicated topic to create.
        if status == "deleted":
            state["migrated_v0_26_0"] = _now_iso_utc()
            try:
                save_state(state_file_path(name), state)
            except Exception as exc:
                skipped_errors.append((name, "state write failed: %s" % exc))
                continue
            skipped_already.append(name)
            continue

        old_thread_id = state.get("workspace_thread_id") or state.get(
            "dedicated_thread_id"
        )
        new_thread_id: Optional[int] = None

        if platform_ready and not state.get("dedicated_thread_id"):
            display_name = state.get("display_name") or name
            new_thread_id = await _create_dedicated_topic(
                ctx.platform, display_name, status, log,
            )
            if new_thread_id is None:
                skipped_errors.append((name, "create_channel returned None"))
                continue

        # Persist new fields atomically BEFORE any further platform side-effect.
        if new_thread_id is not None:
            state["dedicated_thread_id"] = new_thread_id
        state.setdefault("drain_timeout_seconds", 3600)
        state["migrated_v0_26_0"] = _now_iso_utc()

        try:
            save_state(state_file_path(name), state)
        except Exception as exc:
            skipped_errors.append((name, "state write failed: %s" % exc))
            log.error(
                "migration v0_26_0: failed to persist new state for '%s': %s",
                name, exc, exc_info=True,
            )
            continue

        # Re-point queue entry so next scheduler tick delivers to the new topic.
        if new_thread_id is not None:
            try:
                from topics import _update_queue_entry_thread_id
                _update_queue_entry_thread_id(name, new_thread_id)
            except Exception as exc:
                log.warning(
                    "migration v0_26_0: queue re-point failed for '%s': %s",
                    name, exc,
                )

        # Journal seed — every migrated task gets one entry with full provenance.
        _seed_journal_entry(
            journal_path,
            name,
            {
                "old_thread_id": old_thread_id,
                "new_thread_id": new_thread_id,
                "status_at_migration": status,
            },
            log,
        )

        # Retroactively pin an awaiting-input message if the task is paused
        # on a question. Best-effort — failure here does not unwind the
        # migration (the reminder loop in the scheduler will surface the
        # question on the next cycle anyway).
        awaiting_question = state.get("awaiting_question")
        if (
            platform_ready
            and new_thread_id is not None
            and status in ("awaiting_input", "awaiting-input")
            and awaiting_question
        ):
            try:
                parent_chat = _resolve_parent_chat_id(
                    ctx.manager, state.get("parent_workspace") or "",
                )
                if parent_chat is not None:
                    msg = (
                        "⏸ Awaiting your reply on *%s*:\n\n%s\n\n"
                        "Reply in this topic to resume the task."
                        % (name, awaiting_question)
                    )
                    sent = await ctx.platform.send_message(
                        chat_id=parent_chat,
                        text=msg,
                        thread_id=new_thread_id,
                        parse_mode="markdown",
                    )
                    msg_id = None
                    if isinstance(sent, dict):
                        msg_id = sent.get("message_id")
                    elif sent is not None:
                        msg_id = getattr(sent, "message_id", None)
                    if msg_id is not None:
                        try:
                            await ctx.platform.pin_message(
                                chat_id=parent_chat,
                                thread_id=new_thread_id,
                                message_id=msg_id,
                            )
                            state["awaiting_pinned_msg_id"] = msg_id
                            save_state(state_file_path(name), state)
                        except Exception as exc:
                            log.warning(
                                "migration v0_26_0: pin failed for '%s': %s",
                                name, exc,
                            )
            except Exception as exc:
                log.warning(
                    "migration v0_26_0: retroactive awaiting-pin failed for '%s': %s",
                    name, exc,
                )

        migrated.append(name)

    log.info(
        "migration v0_26_0: migrated=%d skipped_already=%d skipped_error=%d",
        len(migrated), len(skipped_already), len(skipped_errors),
    )

    if skipped_errors:
        for nm, reason in skipped_errors:
            log.error(
                "migration v0_26_0: skipped '%s' (%s)", nm, reason,
            )
        failed = ", ".join("%s (%s)" % (nm, reason) for nm, reason in skipped_errors)
        raise RuntimeError(
            "migration v0_26_0: %d task(s) could not be migrated: %s"
            % (len(skipped_errors), failed)
        )

    _write_done_marker(done_marker, log)


MIGRATION = Migration(
    from_version="0.25.1",
    to_version="0.26.0",
    description=(
        "continuous-task dedicated topics, event journal, state-machine "
        "formalisation, lock heartbeat, drain-on-close, orphan backoff"
    ),
    upgrade=upgrade,
)
