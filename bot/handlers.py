"""Robyx — Command and message handlers (platform-agnostic)."""

import asyncio
import functools
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agents import AgentManager, format_age
from ai_backend import AIBackend
from ai_invoke import (
    CREATE_WORKSPACE_PATTERN,
    AGENT_INSTRUCTIONS_PATTERN,
    CLOSE_WORKSPACE_PATTERN,
    CREATE_CONTINUOUS_PATTERN,
    CONTINUOUS_PROGRAM_PATTERN,
    CREATE_SPECIALIST_PATTERN,
    FOCUS_OFF_PATTERN,
    FOCUS_PATTERN,
    REMIND_PATTERN,
    RESTART_PATTERN,
    SEND_IMAGE_PATTERN,
    SILENT_PATTERN,
    SPECIALIST_INSTRUCTIONS_PATTERN,
    TTS_SUMMARY_PATTERN,
    handle_delegations,
    handle_focus_commands,
    handle_specialist_requests,
    invoke_ai,
    parse_remind_attrs,
    parse_remind_when,
    split_message,
)


_EXECUTIVE_MARKERS = (
    ("FOCUS_OFF", FOCUS_OFF_PATTERN),
    ("FOCUS", FOCUS_PATTERN),
    ("RESTART", RESTART_PATTERN),
    ("CREATE_WORKSPACE", CREATE_WORKSPACE_PATTERN),
    ("AGENT_INSTRUCTIONS", AGENT_INSTRUCTIONS_PATTERN),
    ("CLOSE_WORKSPACE", CLOSE_WORKSPACE_PATTERN),
    ("CREATE_CONTINUOUS", CREATE_CONTINUOUS_PATTERN),
    ("CONTINUOUS_PROGRAM", CONTINUOUS_PROGRAM_PATTERN),
    ("CREATE_SPECIALIST", CREATE_SPECIALIST_PATTERN),
    ("SPECIALIST_INSTRUCTIONS", SPECIALIST_INSTRUCTIONS_PATTERN),
    ("SEND_IMAGE", SEND_IMAGE_PATTERN),
    ("REMIND", REMIND_PATTERN),
)


def _strip_executive_markers(response: str, agent_name: str) -> str:
    """Remove every system-level command marker from ``response``.

    Used when the originating message lacks executive authorization
    (e.g. PARTICIPANT in a collab workspace). Logs each marker dropped
    so prompt-injection attempts surface in the bot log.
    """
    if not response:
        return response
    for label, pattern in _EXECUTIVE_MARKERS:
        if pattern.search(response):
            log.warning(
                "Dropped [%s] marker from non-executive response by [%s]",
                label, agent_name,
            )
            response = pattern.sub("", response)
    return re.sub(r"\n{3,}", "\n\n", response).strip()
import config as _config
from config_updates import apply_env_updates, parse_direct_env_updates
from config import WORKSPACE
from scheduler import add_reminder, add_task as _timed_add_task
from i18n import STRINGS
from topics import create_workspace, close_workspace, create_specialist
from updater import (
    check_for_updates,
    get_current_version,
    get_pending_update,
    apply_update,
    restart_service,
)
from collaborative import CollabStore, CollabWorkspace
from voice import is_available as voice_available, transcribe_voice

log = logging.getLogger("robyx.handlers")


_background_tasks: set[asyncio.Task] = set()


def _spawn_tracked(coro, *, name: str | None = None) -> asyncio.Task:
    """Spawn a background asyncio task with exception logging and GC protection.

    Plain ``asyncio.create_task`` returns a task that can be garbage-collected
    while still pending (emitting a runtime warning), and silently swallows
    exceptions into asyncio's default handler. This helper keeps a strong
    reference until completion and routes exceptions to our logger.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error(
                "Background task %s raised: %s",
                t.get_name(), exc, exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


async def _safe_send(platform, chat_id, text, thread_id=None):
    """Send a message with a plain-text fallback if markdown rendering fails.

    Guarantees that *something* reaches the user whenever the underlying
    transport accepts at least plain text. Never raises.
    """
    try:
        await platform.send_message(
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            parse_mode="markdown",
        )
        return True
    except Exception as e:
        log.warning("Markdown send failed, retrying as plain text: %s", e)
    try:
        await platform.send_message(
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
        )
        return True
    except Exception as e:
        log.error("Plain-text send also failed: %s", e, exc_info=True)
        return False


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(platform, msg, msg_ref):
        if not platform.is_owner(msg.user_id):
            log.warning("Rejected non-owner message: user=%s", msg.user_id)
            await platform.reply(msg_ref, STRINGS["unauthorized"])
            return
        return await func(platform, msg, msg_ref)
    return wrapper


def make_handlers(manager: AgentManager, backend: AIBackend, collab_store: CollabStore | None = None):
    """Return all handler functions bound to a given AgentManager and AI backend."""

    @owner_only
    async def cmd_help(platform, msg, msg_ref):
        focus_info = ""
        if manager.focused_agent:
            focus_info = "\n\nFocus active: *%s*" % manager.focused_agent
        await platform.reply(
            msg_ref,
            STRINGS["help_text"] + focus_info,
            parse_mode="markdown",
        )

    @owner_only
    async def cmd_workspaces(platform, msg, msg_ref):
        workspaces = manager.list_workspaces()
        if not workspaces:
            await platform.reply(msg_ref, STRINGS["no_workspaces"])
            return
        lines = [STRINGS["workspaces_title"]]
        for a in workspaces:
            icon = "..." if a.busy else "o"
            age = format_age(a.last_used)
            focus = " *" if manager.focused_agent == a.name else ""
            lines.append(
                "%s *%s*%s — %s\n    msgs: %d, last: %s"
                % (icon, a.name, focus, a.description, a.message_count, age)
            )
        await platform.reply(msg_ref, "\n".join(lines), parse_mode="markdown")

    @owner_only
    async def cmd_specialists(platform, msg, msg_ref):
        specialists = manager.list_specialists()
        if not specialists:
            await platform.reply(msg_ref, STRINGS["no_specialists"])
            return
        lines = [STRINGS["specialists_title"]]
        for a in specialists:
            icon = "..." if a.busy else "o"
            lines.append("%s *%s* — %s" % (icon, a.name, a.description))
        await platform.reply(msg_ref, "\n".join(lines), parse_mode="markdown")

    @owner_only
    async def cmd_status(platform, msg, msg_ref):
        summary = manager.get_status_summary()
        agents = manager.list_active()
        focus = " (focus: %s)" % manager.focused_agent if manager.focused_agent else ""
        header = "*Robyx Status* — %d agents%s\n\n" % (len(agents), focus)
        await platform.reply(msg_ref, header + summary, parse_mode="markdown")

    @owner_only
    async def cmd_reset(platform, msg, msg_ref):
        if not msg.args:
            await platform.reply(msg_ref, STRINGS["reset_usage"])
            return
        name = msg.args[0].lower()
        agent = manager.get(name)
        if agent:
            agent.session_id = str(uuid.uuid4())
            agent.message_count = 0
            agent.session_started = False
            manager.save_state()
            await platform.reply(
                msg_ref,
                STRINGS["agent_reset"] % name,
                parse_mode="markdown",
            )
        else:
            await platform.reply(msg_ref, STRINGS["agent_not_found"] % name)

    @owner_only
    async def cmd_focus(platform, msg, msg_ref):
        if not msg.args:
            if manager.focused_agent:
                await platform.reply(
                    msg_ref,
                    STRINGS["focus_active"] % manager.focused_agent,
                    parse_mode="markdown",
                )
            else:
                await platform.reply(msg_ref, STRINGS["focus_none"])
            return

        name = msg.args[0].lower()
        if name == "off":
            old = manager.focused_agent
            manager.clear_focus()
            suffix = " (was: %s)" % old if old else ""
            await platform.reply(
                msg_ref,
                STRINGS["focus_off_was"] % suffix,
                parse_mode="markdown",
            )
        else:
            agent = manager.get(name)
            if agent:
                manager.set_focus(name)
                await platform.reply(
                    msg_ref,
                    STRINGS["focus_on"] % (name, name),
                    parse_mode="markdown",
                )
            else:
                await platform.reply(msg_ref, STRINGS["agent_not_found"] % name)

    @owner_only
    async def cmd_ping(platform, msg, msg_ref):
        agents = manager.list_active()
        focus = " (focus: %s)" % manager.focused_agent if manager.focused_agent else ""
        await platform.reply(msg_ref, STRINGS["bot_alive"] % (len(agents), focus))

    @owner_only
    async def cmd_checkupdate(platform, msg, msg_ref):
        sent_ref = await platform.reply(
            msg_ref, "Checking for updates (v%s)..." % get_current_version()
        )
        try:
            info = await check_for_updates()
        except Exception as e:
            await platform.edit_message(sent_ref, STRINGS["update_fetch_error"] % str(e))
            return

        if not info:
            await platform.edit_message(sent_ref, STRINGS["update_none"] % get_current_version())
            return

        notes = info["release_notes"]
        body = notes["body"].strip() if notes else "(no release notes)"

        if info["status"] == "incompatible":
            min_compat = notes["min_compatible"] if notes else "unknown"
            text = STRINGS["update_available_incompatible"] % (
                info["current"], info["version"], min_compat,
            )
        elif info["status"] == "breaking":
            text = STRINGS["update_available_breaking"] % (
                info["current"], info["version"], body, info["version"],
            )
        else:
            text = STRINGS["update_available"] % (
                info["current"], info["version"], body,
            )
        await platform.edit_message(sent_ref, text, parse_mode="markdown")

    @owner_only
    async def cmd_doupdate(platform, msg, msg_ref):
        sent_ref = await platform.reply(msg_ref, STRINGS["update_checking_manual"])

        # Check for busy agents
        busy_agents = [a for a in manager.list_active() if a.busy]
        if busy_agents:
            names = ", ".join("*%s*" % a.name for a in busy_agents)
            force = msg.args and msg.args[0] == "force"
            if not force:
                await platform.edit_message(
                    sent_ref,
                    "Update blocked — agents busy: %s\n\n"
                    "Wait for them to finish, or use `/doupdate force` to proceed anyway." % names,
                    parse_mode="markdown",
                )
                return
            await platform.edit_message(
                sent_ref,
                "Forcing update — busy agents (%s) will be interrupted." % names,
                parse_mode="markdown",
            )

        # Check for running scheduled tasks
        from scheduler import get_running_tasks
        running_tasks = [
            (t["name"], t["_pid"]) for t in await get_running_tasks()
        ]
        if running_tasks:
            force = msg.args and msg.args[0] == "force"
            task_list = ", ".join("*%s* (PID %d)" % (n, p) for n, p in running_tasks)
            if not force:
                await platform.edit_message(
                    sent_ref,
                    "Update blocked — scheduled tasks running: %s\n\n"
                    "Wait for them to finish, or use `/doupdate force` to proceed anyway." % task_list,
                    parse_mode="markdown",
                )
                return

        try:
            pending = await get_pending_update()
        except Exception as e:
            await platform.edit_message(
                sent_ref,
                STRINGS["update_failed"] % (str(e), get_current_version()),
            )
            return

        if not pending:
            await platform.edit_message(sent_ref, STRINGS["update_no_pending"])
            return

        version = pending["version"]
        current = pending["current"]

        await platform.edit_message(
            sent_ref,
            STRINGS["update_applying"] % version,
            parse_mode="markdown",
        )

        async def notify_progress(text):
            try:
                await platform.edit_message(
                    sent_ref,
                    "*Updating to v%s...*\n\n%s" % (version, text),
                    parse_mode="markdown",
                )
            except Exception:
                pass

        success, result = await apply_update(version, notify_fn=notify_progress, manager=manager)

        if success:
            await platform.edit_message(
                sent_ref,
                STRINGS["update_success"] % result,
                parse_mode="markdown",
            )
            restart_service()
        else:
            await platform.edit_message(
                sent_ref,
                STRINGS["update_failed"] % (result, current),
                parse_mode="markdown",
            )

    async def _process_and_send(
        agent, message, chat_id, platform, thread_id=None, *, is_executive=True,
    ):
        stop_typing = asyncio.Event()

        async def _typing_loop():
            while not stop_typing.is_set():
                try:
                    await platform.send_typing(chat_id, thread_id)
                except Exception:
                    pass
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(_typing_loop())
        try:
            is_robyx = agent.name == "robyx"
            response = await invoke_ai(
                agent, message, chat_id, platform, manager, backend, is_robyx, thread_id=thread_id,
            )
            manager.save_state()

            # None means the agent was interrupted — skip sending, the
            # user's new message will be processed as a fresh invocation.
            if response is None:
                return

            # Defense-in-depth: if the originating message was non-executive
            # (a participant in a collab workspace), strip every tool-marker
            # that could trigger a state-changing or system-level action.
            # The agent is *told* not to emit these in non-executive context,
            # but a prompt-injection attempt could still slip them through —
            # silently dropping them here closes that gap.
            if not is_executive:
                response = _strip_executive_markers(response, agent.name)
                needs_restart = False
            else:
                # Handle AI-generated commands
                response = await handle_focus_commands(
                    response, chat_id, platform, manager, thread_id=thread_id,
                )

                # Check for restart request before other processing
                needs_restart = bool(RESTART_PATTERN.search(response))
                response = RESTART_PATTERN.sub("", response).strip()

                if is_robyx:
                    response = await handle_delegations(
                        response, chat_id, platform, manager, backend, thread_id=thread_id,
                    )
                    response = await _handle_workspace_commands(response, chat_id, platform, thread_id)
                else:
                    response = await handle_specialist_requests(
                        response, chat_id, platform, manager, backend, agent, thread_id=thread_id,
                    )

                # Outgoing image attachments (only if the agent explicitly emitted
                # [SEND_IMAGE ...] — the system prompt forbids proactive emission).
                response = await _handle_media_commands(
                    response, chat_id, platform, thread_id,
                    agent_work_dir=agent.work_dir,
                )

                # Schedule any [REMIND ...] requests into the reminder engine.
                response = _handle_remind_commands(response, agent, chat_id, thread_id)

            # Strip TTS summary blocks — redundant recap not useful on chat.
            response = TTS_SUMMARY_PATTERN.sub("", response).strip()
            response = re.sub(r'\n{3,}', '\n\n', response)

            # Collaborative agent chose not to respond.
            if SILENT_PATTERN.search(response):
                cleaned = SILENT_PATTERN.sub("", response).strip()
                if not cleaned:
                    log.info("Agent [%s] responded with [SILENT] — suppressing", agent.name)
                    return
                response = cleaned

            await _send_response(chat_id, platform, agent, response, thread_id=thread_id)

            if needs_restart:
                log.info("Restart requested by agent [%s] — restarting service", agent.name)
                await platform.send_message(
                    chat_id=chat_id,
                    text=STRINGS["restart_pending"],
                    thread_id=thread_id,
                    parse_mode="markdown",
                )
                restart_service()
        except Exception as e:
            log.error("Error in _process_and_send for [%s]: %s", agent.name, e, exc_info=True)
            await _safe_send(
                platform,
                chat_id,
                STRINGS["ai_error"] % str(e),
                thread_id=thread_id,
            )
        finally:
            stop_typing.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

    async def _handle_workspace_commands(response, chat_id, platform, thread_id):
        """Parse and execute workspace/specialist creation/closure commands from Robyx."""
        # Handle CREATE_WORKSPACE (supports multiple in one response)
        ws_matches = list(CREATE_WORKSPACE_PATTERN.finditer(response))
        instr_matches = list(AGENT_INSTRUCTIONS_PATTERN.finditer(response))
        if ws_matches:
            response = CREATE_WORKSPACE_PATTERN.sub("", response)
            response = AGENT_INSTRUCTIONS_PATTERN.sub("", response)
            response = response.strip()

            for i, ws_match in enumerate(ws_matches):
                ws_name = ws_match.group(1)
                ws_type = ws_match.group(2)
                ws_freq = ws_match.group(3)
                ws_model = ws_match.group(4)
                ws_sched = ws_match.group(5)
                instructions = instr_matches[i].group(1).strip() if i < len(instr_matches) else ""

                if len(ws_matches) > 1:
                    try:
                        await platform.send_message(
                            chat_id=chat_id,
                            text="_%d/%d — Creating workspace %s..._" % (i + 1, len(ws_matches), ws_name),
                            thread_id=thread_id,
                            parse_mode="markdown",
                        )
                    except Exception:
                        pass

                rejection_reason: str | None = None
                try:
                    result = await create_workspace(
                        name=ws_name,
                        task_type=ws_type,
                        frequency=ws_freq,
                        model=ws_model,
                        scheduled_at=ws_sched,
                        instructions=instructions,
                        manager=manager,
                        work_dir=str(WORKSPACE),
                        platform=platform,
                    )
                except ValueError as e:
                    # Reserved name / duplicate name — surface the reason
                    # verbatim so Robyx can explain it to the user instead
                    # of the generic failure message.
                    log.warning("create_workspace(%s) rejected: %s", ws_name, e)
                    result = None
                    rejection_reason = str(e)
                except Exception as e:
                    log.error("create_workspace(%s) raised: %s", ws_name, e, exc_info=True)
                    result = None

                if result:
                    response += "\n\nWorkspace *%s* created (topic #%s)." % (
                        result["display_name"], result["thread_id"]
                    )
                elif rejection_reason:
                    response += "\n\nWorkspace *%s* not created: %s." % (
                        ws_name, rejection_reason,
                    )
                else:
                    response += "\n\nFailed to create workspace *%s*." % ws_name

        # Handle CLOSE_WORKSPACE
        close_match = CLOSE_WORKSPACE_PATTERN.search(response)
        if close_match:
            ws_name = close_match.group(1).lower()
            success = await close_workspace(ws_name, manager, platform=platform)
            response = CLOSE_WORKSPACE_PATTERN.sub("", response).strip()
            if success:
                response += "\n\nWorkspace *%s* closed." % ws_name
            else:
                response += "\n\nWorkspace '%s' not found." % ws_name

        # Handle CREATE_SPECIALIST (supports multiple in one response)
        spec_matches = list(CREATE_SPECIALIST_PATTERN.finditer(response))
        spec_instr_matches = list(SPECIALIST_INSTRUCTIONS_PATTERN.finditer(response))
        if spec_matches:
            response = CREATE_SPECIALIST_PATTERN.sub("", response)
            response = SPECIALIST_INSTRUCTIONS_PATTERN.sub("", response)
            response = response.strip()

            for i, spec_match in enumerate(spec_matches):
                spec_name = spec_match.group(1)
                spec_model = spec_match.group(2)
                instructions = spec_instr_matches[i].group(1).strip() if i < len(spec_instr_matches) else ""

                rejection_reason = None
                try:
                    result = await create_specialist(
                        name=spec_name,
                        model=spec_model,
                        instructions=instructions,
                        manager=manager,
                        work_dir=str(WORKSPACE),
                        platform=platform,
                    )
                except ValueError as e:
                    log.warning("create_specialist(%s) rejected: %s", spec_name, e)
                    result = None
                    rejection_reason = str(e)
                except Exception as e:
                    log.error("create_specialist(%s) raised: %s", spec_name, e, exc_info=True)
                    result = None

                if result:
                    response += "\n\nSpecialist *%s* created (topic #%s)." % (
                        result["display_name"], result["thread_id"]
                    )
                elif rejection_reason:
                    response += "\n\nSpecialist *%s* not created: %s." % (
                        spec_name, rejection_reason,
                    )
                else:
                    response += "\n\nFailed to create specialist *%s*." % spec_name

        # Handle CREATE_CONTINUOUS
        cont_match = CREATE_CONTINUOUS_PATTERN.search(response)
        prog_match = CONTINUOUS_PROGRAM_PATTERN.search(response)
        if cont_match and prog_match:
            response = CREATE_CONTINUOUS_PATTERN.sub("", response)
            response = CONTINUOUS_PROGRAM_PATTERN.sub("", response)
            response = response.strip()

            cont_name = cont_match.group(1)
            cont_work_dir = cont_match.group(2)

            # Validate work_dir stays under WORKSPACE to prevent path traversal
            # via prompt injection in AI responses.
            try:
                resolved_wd = Path(cont_work_dir).resolve()
                if not str(resolved_wd).startswith(str(WORKSPACE.resolve())):
                    log.warning(
                        "Continuous task %s: work_dir %r escapes WORKSPACE — rejected",
                        cont_name, cont_work_dir,
                    )
                    response += "\n\nFailed to create continuous task: work_dir outside workspace."
                    return response
            except (OSError, ValueError):
                response += "\n\nFailed to create continuous task: invalid work_dir."
                return response

            try:
                import json as _json
                program = _json.loads(prog_match.group(1).strip())
            except (ValueError, TypeError) as e:
                log.error("Invalid CONTINUOUS_PROGRAM JSON: %s", e)
                response += "\n\nFailed to create continuous task: invalid program JSON."
                return response

            from topics import create_continuous_workspace

            parent_agent = manager.get_by_thread(thread_id)
            if parent_agent:
                parent_ws_name = parent_agent.name
            else:
                log.warning(
                    "Continuous task %s: thread_id=%s has no mapped "
                    "workspace; reparenting to 'robyx'",
                    cont_name, thread_id,
                )
                parent_ws_name = "robyx"

            rejection_reason = None
            try:
                result = await create_continuous_workspace(
                    name=cont_name,
                    program=program,
                    work_dir=cont_work_dir,
                    parent_workspace=parent_ws_name,
                    model="powerful",
                    manager=manager,
                    platform=platform,
                )
            except ValueError as e:
                log.warning("create_continuous_workspace(%s) rejected: %s", cont_name, e)
                result = None
                rejection_reason = str(e)
            except Exception as e:
                log.error("create_continuous_workspace(%s) raised: %s", cont_name, e, exc_info=True)
                result = None

            if result:
                response += "\n\n🔄 Continuous task *%s* created (topic #%s, branch `%s`)." % (
                    result["display_name"], result["thread_id"], result["branch"],
                )
            elif rejection_reason:
                response += "\n\nContinuous task *%s* not created: %s." % (cont_name, rejection_reason)
            else:
                response += "\n\nFailed to create continuous task *%s*." % cont_name

        return response

    def _validate_image_path(raw_path: str, agent_work_dir: str | None) -> bool:
        """Return True iff ``raw_path`` resolves under an allowed root.

        Allowed roots: the agent's ``work_dir`` (its own workspace), the bot's
        ``DATA_DIR``, the system tempdir, and — on POSIX — ``/tmp`` (which on
        macOS lives at ``/private/tmp`` and resolves there). Any other path —
        absolute or escaping via ``..`` — is rejected. This prevents a
        prompt-injection ``[SEND_IMAGE path="/etc/passwd"]`` from exfiltrating
        arbitrary files.
        """
        import tempfile as _tempfile
        from config import DATA_DIR as _DATA_DIR
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError):
            return False
        roots: list[Path] = [
            _DATA_DIR.resolve(),
            Path(_tempfile.gettempdir()).resolve(),
        ]
        if os.name == "posix":
            try:
                roots.append(Path("/tmp").resolve())
            except (OSError, ValueError):
                pass
        if agent_work_dir:
            try:
                roots.append(Path(agent_work_dir).resolve())
            except (OSError, ValueError):
                pass
        for root in roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    async def _handle_media_commands(
        response, chat_id, platform, thread_id, *, agent_work_dir: str | None = None,
    ):
        """Parse and execute [SEND_IMAGE path="..." caption="..."] patterns.

        For each match: strip it from the response text, ask the platform
        adapter to upload the file (the adapter handles size limits via
        media.prepare_image_for_upload). On failure, append a short textual
        notice to the response so the user is never left wondering whether
        the image actually arrived.

        Image paths are validated against an allowlist of roots before any
        filesystem access (agent work_dir, DATA_DIR, tmpdir) — agent-supplied
        paths cannot escape the sandbox.

        Multiple images in one response are sent in order.
        """
        matches = list(SEND_IMAGE_PATTERN.finditer(response))
        if not matches:
            return response

        response = SEND_IMAGE_PATTERN.sub("", response).strip()
        errors = []

        for match in matches:
            path = match.group(1)
            caption = (match.group(2) or "").strip() or None
            if not _validate_image_path(path, agent_work_dir):
                log.warning(
                    "SEND_IMAGE rejected: path %r is outside allowed roots", path,
                )
                errors.append(
                    "Refused to send image `%s` (path outside allowed roots)." % path
                )
                continue
            log.info("SEND_IMAGE: path=%s caption=%r", path, caption)
            try:
                result = await platform.send_photo(
                    chat_id=chat_id,
                    path=path,
                    caption=caption,
                    thread_id=thread_id,
                )
            except Exception as e:
                log.error("SEND_IMAGE failed for %s: %s", path, e, exc_info=True)
                errors.append("Failed to send image `%s`: %s" % (path, e))
                continue
            if result is None:
                errors.append("Failed to send image `%s` (see logs)." % path)

        if errors:
            suffix = "\n\n" + "\n".join(errors)
            response = (response + suffix).strip() if response else suffix.strip()

        return response

    def _parse_remind_thread(attrs: dict) -> int | str | None:
        """Extract and coerce the ``thread=`` attribute from reminder attrs."""
        thread_attr = attrs.get("thread")
        if not thread_attr:
            return None
        return int(thread_attr) if re.fullmatch(r"-?\d+", thread_attr) else thread_attr

    def _queue_action_reminder(
        attrs: dict, text: str, fire_at, target_agent_name: str,
        target_thread, agent, now,
    ) -> str | None:
        """Queue an action-mode reminder into the timed task queue.

        Returns an error string on failure, or ``None`` on success.
        """
        target_agent = manager.get(target_agent_name)
        if not target_agent:
            return "Reminder rejected: unknown agent '%s'" % target_agent_name
        if target_agent.agent_type not in ("workspace", "specialist"):
            return (
                "Reminder rejected: '%s' is not a workspace or specialist"
                % target_agent_name
            )

        if target_thread is None:
            target_thread = target_agent.thread_id or None

        agent_file_rel = (
            "specialists/%s.md" % target_agent.name
            if target_agent.agent_type == "specialist"
            else "agents/%s.md" % target_agent.name
        )
        task_entry = {
            "id": "r-" + uuid.uuid4().hex[:8],
            "name": "remind-%s-%s" % (target_agent.name, uuid.uuid4().hex[:6]),
            "agent_file": agent_file_rel,
            "prompt": text,
            "type": "one-shot",
            "scheduled_at": fire_at.isoformat(),
            "status": "pending",
            "model": target_agent.model or "balanced",
            "thread_id": str(target_thread) if target_thread is not None else "",
            "description": "REMIND action → @%s" % target_agent.name,
            "created_at": now.isoformat(),
            "source": "remind",
        }
        try:
            _timed_add_task(task_entry)
            log.info(
                "REMIND action queued: id=%s fire_at=%s target=@%s "
                "thread=%s prompt=%r (from=%s)",
                task_entry["id"], task_entry["scheduled_at"],
                target_agent.name, target_thread, text[:60], agent.name,
            )
        except (ValueError, OSError) as e:
            log.error("REMIND action enqueue failed: %s", e, exc_info=True)
            return "Reminder could not be scheduled: %s" % e
        return None

    def _queue_text_reminder(
        text: str, fire_at, chat_id, target_thread, agent, now,
    ) -> str | None:
        """Queue a text-mode reminder into the unified queue.

        Returns an error string on failure, or ``None`` on success.
        """
        entry = {
            "id": "r-" + uuid.uuid4().hex[:8],
            "chat_id": chat_id,
            "message": text,
            "fire_at": fire_at.isoformat(),
            "thread_id": target_thread,
            "created_at": now.isoformat(),
            "status": "pending",
        }
        try:
            add_reminder(entry)
            log.info(
                "REMIND queued: id=%s fire_at=%s thread=%s text=%r (agent=%s)",
                entry["id"], entry["fire_at"], target_thread,
                text[:60], agent.name,
            )
        except (OSError, ValueError) as e:
            log.error("REMIND append failed: %s", e, exc_info=True)
            return "Reminder could not be saved: %s" % e
        return None

    def _handle_remind_commands(response, agent, chat_id, thread_id):
        """Parse and queue ``[REMIND ...]`` requests from an agent response.

        Single dispatch path: parse every match, validate ``text`` and
        ``fire_at``, then route to the right queue based on whether an
        ``agent="name"`` attribute was provided.

        * **text mode** (no ``agent=``): append to the reminder queue via
          ``add_reminder``; the scheduler delivers plain text at
          ``fire_at``. Thread defaults to the channel the caller lives in.
        * **action mode** (``agent="..."``): append to the timed-task
          queue; the scheduler spawns the named workspace or specialist
          at ``fire_at`` with ``text`` as the prompt. Thread defaults to
          the target agent's own channel (so the output lands there).

        Validation failures are appended as inline notices — never
        silently dropped — so the user sees exactly which reminders
        were rejected and why.
        """
        matches = list(REMIND_PATTERN.finditer(response))
        if not matches:
            return response

        response = REMIND_PATTERN.sub("", response).strip()
        errors: list[str] = []
        now = datetime.now(timezone.utc)

        for match in matches:
            attrs = parse_remind_attrs(match.group(1))
            text = attrs.get("text", "").strip()
            if not text:
                errors.append("Reminder rejected: missing `text`.")
                continue
            try:
                fire_at = parse_remind_when(
                    attrs.get("at"), attrs.get("in"), now=now,
                )
            except ValueError as e:
                errors.append("Reminder rejected: %s" % e)
                continue

            target_agent_name = (attrs.get("agent") or "").strip().lower()
            target_thread = _parse_remind_thread(attrs)

            if target_agent_name:
                if target_thread is None:
                    target_thread = thread_id
                err = _queue_action_reminder(
                    attrs, text, fire_at, target_agent_name,
                    target_thread, agent, now,
                )
            else:
                if target_thread is None:
                    target_thread = thread_id
                err = _queue_text_reminder(
                    text, fire_at, chat_id, target_thread, agent, now,
                )
            if err:
                errors.append(err)

        if errors:
            suffix = "\n\n" + "\n".join(errors)
            response = (response + suffix).strip() if response else suffix.strip()

        return response

    async def _send_response(chat_id, platform, agent, response, thread_id=None):
        if agent.name == "robyx":
            tag = "*Robyx*"
        elif agent.agent_type == "specialist":
            tag = "*%s* [specialist]" % agent.name
        else:
            tag = "*%s*" % agent.name

        if not response or not response.strip():
            log.warning("Empty response from [%s] after stripping patterns", agent.name)
            response = STRINGS["ai_empty"]

        for chunk in split_message(response):
            try:
                await platform.send_message(
                    chat_id=chat_id,
                    text="%s\n\n%s" % (tag, chunk),
                    thread_id=thread_id,
                    parse_mode="markdown",
                )
            except Exception:
                await platform.send_message(
                    chat_id=chat_id,
                    text="[%s]\n\n%s" % (agent.name, chunk),
                    thread_id=thread_id,
                )

    @owner_only
    async def handle_voice(platform, msg, msg_ref):
        if not msg.voice_file_id:
            return

        # Early check: if Whisper is not configured, tell the user immediately
        if not voice_available():
            await platform.reply(msg_ref, STRINGS["voice_no_key"])
            return

        await platform.send_typing(msg.chat_id, msg.thread_id)
        tmp_path = await platform.download_voice(msg.voice_file_id)

        text, error = await transcribe_voice(tmp_path)
        os.unlink(tmp_path)

        if error or not text:
            await platform.reply(msg_ref, error or STRINGS["ai_empty"])
            return

        # Show transcription so the user can see what was said without replaying
        await platform.reply(
            msg_ref,
            STRINGS["voice_transcript"] % text,
            parse_mode="markdown",
        )

        await _route_and_process(platform, msg, msg_ref, text)

    async def _route_and_process(platform, msg, msg_ref, text):
        """Resolve which agent should handle *text* and dispatch.

        Routing rules (in order):
        1. Message arrives from a forum topic / thread that maps to a known
           workspace/specialist agent → route to that agent, stay in-thread.
        2. Message arrives from the platform's *main* destination → fall back
           to @mention / focus / Robyx; reply in-place (no thread rewrite).
        3. Message arrives from an un-mapped topic/thread → do NOT invoke any
           AI. Reply in the same thread telling the user the topic is orphaned.
           This prevents silent migration of the conversation to #general,
           which was the old "Robyx stops typing and typing appears in general"
           failure mode.
        """
        thread_id = msg.thread_id
        chat_id = msg.chat_id
        is_main = platform.is_main_thread(chat_id, thread_id)

        if is_main:
            agent, message = manager.resolve_agent(text)
        else:
            route_id = thread_id if thread_id is not None else chat_id
            topic_agent = manager.get_by_thread(route_id)
            if topic_agent is None:
                log.warning(
                    "Message in unmapped channel/thread chat=%s thread=%s — replying with hint",
                    chat_id, thread_id,
                )
                await platform.reply(
                    msg_ref,
                    STRINGS["unmapped_topic"],
                    parse_mode="markdown",
                )
                return
            agent = topic_agent
            message = text

        if not message.strip():
            await platform.reply(msg_ref, STRINGS["empty_message"])
            return

        await _process_and_send(agent, message, chat_id, platform, thread_id=thread_id)

    async def handle_message(platform, msg, msg_ref):
        text = msg.text
        if not text:
            return

        # ── Collaborative workspace messages bypass owner_only ──
        if collab_store is not None:
            collab_ws = collab_store.get_by_chat_id(msg.chat_id)
            if collab_ws:
                await _handle_collaborative_message(platform, msg, msg_ref, collab_ws)
                return

        # ── Standard HQ path: owner_only ──
        if not platform.is_owner(msg.user_id):
            log.warning("Rejected non-owner message: user=%s", msg.user_id)
            await platform.reply(msg_ref, STRINGS["unauthorized"])
            return

        # Treat bare "help" in main thread as /help command
        if text.strip().lower() == "help" and platform.is_main_thread(msg.chat_id, msg.thread_id):
            await cmd_help(platform, msg, msg_ref)
            return

        direct_updates = parse_direct_env_updates(text)
        if direct_updates:
            keys = ", ".join("`%s`" % key for key in direct_updates)
            try:
                apply_env_updates(_config.PROJECT_ROOT / ".env", direct_updates)
            except Exception as e:
                log.error(
                    "Direct config update failed for keys [%s]: %s",
                    ", ".join(sorted(direct_updates)),
                    e,
                    exc_info=True,
                )
                await _safe_send(
                    platform,
                    msg.chat_id,
                    STRINGS["ai_error"] % str(e),
                    thread_id=msg.thread_id,
                )
                return

            log.info("Applied direct config update for keys: %s", ", ".join(sorted(direct_updates)))
            await platform.reply(
                msg_ref,
                STRINGS["config_updated"] % keys,
                parse_mode="markdown",
            )
            await platform.send_message(
                chat_id=msg.chat_id,
                text=STRINGS["restart_pending"],
                thread_id=msg.thread_id,
                parse_mode="markdown",
            )
            restart_service()
            return

        log.info(
            "Handling message: user=%s chat=%s thread=%s chars=%d",
            msg.user_id, msg.chat_id, msg.thread_id, len(text),
        )

        async def _early_typing():
            try:
                await platform.send_typing(msg.chat_id, msg.thread_id)
            except Exception as e:
                log.warning(
                    "Early typing send failed (chat=%s thread=%s): %s",
                    msg.chat_id, msg.thread_id, e,
                )

        _spawn_tracked(_early_typing(), name="early_typing")

        await _route_and_process(platform, msg, msg_ref, text)

    async def _handle_collaborative_message(platform, msg, msg_ref, collab_ws):
        """Route a message from a collaborative workspace group.

        Authorization is role-based: owner and operators can give executive
        instructions; participants can converse but the agent treats their
        messages as non-executive context.

        Lifecycle commands (/promote, /demote, /role, /mode, /close) are
        intercepted here before reaching the AI agent.
        """
        from authorization import get_user_role, can_send_executive, can_manage_roles
        from collaborative import Role

        # OWNER_ID is None when unconfigured: pass through as-is so that
        # get_user_role never matches an unset owner. Membership of the
        # Telegram group is the only authorization signal we have for
        # otherwise-unknown users.
        owner_id = getattr(_config, "OWNER_ID", None)

        role, _ = get_user_role(
            msg.user_id, msg.chat_id, collab_store, owner_id=owner_id,
        )

        # Unknown senders default to PARTICIPANT in-memory only — we trust
        # the OWNER's manual Telegram-group membership. Roles are NEVER
        # mutated by the agent: only /promote and /demote (owner-driven)
        # change persisted roles.
        if role is None:
            role = Role.PARTICIPANT
            log.info(
                "Collaborative [%s]: unknown sender defaulted to PARTICIPANT "
                "(user=%s chat=%s)",
                collab_ws.agent_name, msg.user_id, msg.chat_id,
            )

        # ── Lifecycle commands (intercepted before AI) ──
        text = (msg.text or "").strip()
        if text.startswith("/"):
            handled = await _handle_collab_command(
                platform, msg, msg_ref, collab_ws, role,
            )
            if handled:
                return

        agent = manager.get(collab_ws.agent_name)
        if not agent:
            log.warning(
                "Collaborative workspace %s references unknown agent %s",
                collab_ws.id, collab_ws.agent_name,
            )
            return

        user_display = msg.user_name or str(msg.user_id)
        is_executive = can_send_executive(role)

        # Passive mode: respond only to explicit @bot mentions or to
        # messages from executive users (owner/operator). When the
        # platform adapter can't report its handle, mentions are
        # undetectable — fall back to executive-only to fail closed.
        if collab_ws.interaction_mode == "passive":
            bot_username = platform.bot_username
            mentioned = bool(
                bot_username and ("@%s" % bot_username) in (msg.text or "")
            )
            if not mentioned and not is_executive:
                return

        exec_tag = " [EXECUTIVE]" if is_executive else ""
        formatted_text = "[%s (%s)%s] %s" % (
            user_display, role.value, exec_tag, msg.text,
        )

        log.info(
            "Collaborative message: user=%s (%s) chat=%s agent=%s chars=%d",
            msg.user_id, role.value, msg.chat_id, collab_ws.agent_name, len(msg.text),
        )

        async def _early_typing():
            try:
                await platform.send_typing(msg.chat_id, msg.thread_id)
            except Exception:
                pass

        _spawn_tracked(_early_typing(), name="early_typing_collab")

        await _process_and_send(
            agent, formatted_text, msg.chat_id, platform,
            thread_id=msg.thread_id, is_executive=is_executive,
        )

    async def _handle_collab_command(platform, msg, msg_ref, collab_ws, role):
        """Handle lifecycle commands inside a collaborative workspace group.

        Returns True if the message was a recognized command (handled or
        rejected), False if it should be passed to the AI agent.
        """
        from authorization import can_manage_roles
        from collaborative import Role

        text = (msg.text or "").strip()
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/promote":
            if not can_manage_roles(role):
                await platform.reply(msg_ref, STRINGS["collab_not_owner"])
                return True
            if not arg:
                await platform.reply(msg_ref, STRINGS["collab_promote_usage"])
                return True
            target_id = _parse_user_id(arg)
            if target_id is None:
                await platform.reply(msg_ref, STRINGS["collab_promote_usage"])
                return True
            return await _collab_promote(platform, msg_ref, collab_ws, target_id)

        if cmd == "/demote":
            if not can_manage_roles(role):
                await platform.reply(msg_ref, STRINGS["collab_not_owner"])
                return True
            if not arg:
                await platform.reply(msg_ref, STRINGS["collab_demote_usage"])
                return True
            target_id = _parse_user_id(arg)
            if target_id is None:
                await platform.reply(msg_ref, STRINGS["collab_demote_usage"])
                return True
            return await _collab_demote(platform, msg_ref, collab_ws, target_id)

        if cmd == "/role" or cmd == "/roles":
            return await _collab_show_roles(platform, msg_ref, collab_ws)

        if cmd == "/mode":
            if not can_manage_roles(role):
                await platform.reply(msg_ref, STRINGS["collab_not_owner"])
                return True
            if arg not in ("intelligent", "passive"):
                await platform.reply(msg_ref, STRINGS["collab_mode_usage"])
                return True
            collab_store.update_interaction_mode(collab_ws.id, arg)
            await platform.reply(
                msg_ref,
                STRINGS["collab_mode_changed"] % arg,
                parse_mode="markdown",
            )
            return True

        if cmd == "/close":
            from authorization import can_close_workspace
            if not can_close_workspace(
                role, msg.user_id, collab_ws,
                owner_id=getattr(_config, "OWNER_ID", None),
            ):
                await platform.reply(msg_ref, STRINGS["collab_close_denied"])
                return True
            collab_store.close(collab_ws.id)
            await platform.reply(
                msg_ref,
                STRINGS["collab_close_confirm"] % collab_ws.display_name,
                parse_mode="markdown",
            )
            try:
                await platform.send_message(
                    chat_id=_config.CHAT_ID or 0,
                    text="Collaborative workspace *%s* has been closed." % collab_ws.display_name,
                    thread_id=getattr(platform, "control_room_id", None),
                    parse_mode="markdown",
                )
            except Exception as e:
                log.warning("Failed to notify HQ about collab close: %s", e)
            return True

        return False

    def _parse_user_id(text: str) -> int | None:
        text = text.strip().lstrip("@")
        try:
            return int(text)
        except ValueError:
            return None

    async def _collab_promote(platform, msg_ref, collab_ws, target_id):
        from collaborative import Role
        current = collab_ws.get_role(target_id)
        if current is None:
            await platform.reply(
                msg_ref, STRINGS["collab_user_not_found"] % target_id,
            )
            return True
        if current == Role.OWNER:
            await platform.reply(msg_ref, STRINGS["collab_cannot_change_owner"])
            return True
        if current == Role.OPERATOR:
            await platform.reply(
                msg_ref,
                STRINGS["collab_already_role"] % (target_id, "operator"),
                parse_mode="markdown",
            )
            return True
        collab_store.update_roles(collab_ws.id, target_id, Role.OPERATOR)
        await platform.reply(
            msg_ref,
            STRINGS["collab_promoted"] % (target_id, "operator"),
            parse_mode="markdown",
        )
        return True

    async def _collab_demote(platform, msg_ref, collab_ws, target_id):
        from collaborative import Role
        current = collab_ws.get_role(target_id)
        if current is None:
            await platform.reply(
                msg_ref, STRINGS["collab_user_not_found"] % target_id,
            )
            return True
        if current == Role.OWNER:
            await platform.reply(msg_ref, STRINGS["collab_cannot_change_owner"])
            return True
        if current == Role.PARTICIPANT:
            await platform.reply(
                msg_ref,
                STRINGS["collab_already_role"] % (target_id, "participant"),
                parse_mode="markdown",
            )
            return True
        collab_store.update_roles(collab_ws.id, target_id, Role.PARTICIPANT)
        await platform.reply(
            msg_ref,
            STRINGS["collab_demoted"] % (target_id, "participant"),
            parse_mode="markdown",
        )
        return True

    async def _collab_show_roles(platform, msg_ref, collab_ws):
        from collaborative import Role
        users = collab_ws.list_users()
        if not users:
            await platform.reply(msg_ref, STRINGS["collab_no_users"])
            return True
        lines = [STRINGS["collab_roles_title"]]
        role_order = {Role.OWNER: 0, Role.OPERATOR: 1, Role.PARTICIPANT: 2}
        for uid, r in sorted(users, key=lambda x: role_order.get(x[1], 99)):
            lines.append("  %s — *%s*" % (uid, r.value))
        await platform.reply(msg_ref, "\n".join(lines), parse_mode="markdown")
        return True

    # ── Collaborative workspace: bot added to a new group ──

    async def collab_bot_added(platform, chat, added_by):
        """Handle the bot being added to a new Telegram group.

        Two flows:
        A) An agent was told in advance (status="pending") → match and configure.
        B) No pending request → notify Robyx in HQ so the user can decide.
        """
        if collab_store is None:
            return

        chat_id = chat.id
        added_by_id = added_by.id if added_by else None
        chat_title = getattr(chat, "title", None) or "Unnamed group"

        # Flow A: only match pending workspaces explicitly bound to the
        # user who added the bot. Without this binding, *any* pending
        # workspace would attach to *any* group, letting an outsider
        # hijack one Robyx provisioned for a different chat.
        pending = (
            collab_store.list_pending_for_creator(added_by_id)
            if added_by_id is not None else []
        )

        if pending:
            ws = sorted(pending, key=lambda w: w.created_at, reverse=True)[0]
            if not collab_store.update_chat_id(
                ws.id, chat_id, expected_creator_id=added_by_id,
            ):
                log.warning(
                    "Could not bind pending workspace %s to chat %s (creator=%s)",
                    ws.id, chat_id, added_by_id,
                )
                return
            collab_store.update_roles(ws.id, added_by_id, _collab_role("owner"))

            # Generate invite link
            try:
                link = await platform.get_invite_link(chat_id)
                if link:
                    collab_store.update_invite_link(ws.id, link)
            except Exception as e:
                log.warning("Failed to generate invite link for collab %s: %s", ws.id, e)

            # Send welcome message in the new group
            try:
                await platform.send_message(
                    chat_id=chat_id,
                    text="*%s* -- collaborative workspace is ready.\n\n"
                         "I'm the agent for this workspace. "
                         "Owner and operators can give me executive instructions; "
                         "other participants can talk and I'll help when appropriate."
                         % ws.display_name,
                    parse_mode="markdown",
                )
            except Exception as e:
                log.warning("Failed to send collab welcome: %s", e)

            # Notify in HQ
            link_text = "\nInvite link: %s" % ws.invite_link if ws.invite_link else ""
            try:
                await platform.send_message(
                    chat_id=_config.CHAT_ID if hasattr(_config, "CHAT_ID") else 0,
                    text="*Collaborative workspace configured*\n\n"
                         "Workspace *%s* is now linked to group _%s_ (chat_id: %d).%s"
                         % (ws.display_name, chat_title, chat_id, link_text),
                    thread_id=platform.control_room_id,
                    parse_mode="markdown",
                )
            except Exception as e:
                log.warning("Failed to send collab HQ notification: %s", e)

            log.info(
                "Collaborative workspace [%s] configured: chat_id=%d title=%r",
                ws.name, chat_id, chat_title,
            )
            return

        # Flow B: no pending request — create a provisional workspace and
        # ask directly in the group what the user wants to do.
        log.info(
            "Bot added to unknown group: chat_id=%d title=%r by=%s — starting in-group setup",
            chat_id, chat_title, added_by_id,
        )
        import re as _re
        safe_name = _re.sub(r'[^a-z0-9-]', '-', chat_title.lower().strip()).strip('-') or "collab"
        safe_name = "collab-%s" % safe_name

        # Avoid name collisions
        base_name = safe_name
        counter = 1
        while manager.get(safe_name):
            safe_name = "%s-%d" % (base_name, counter)
            counter += 1

        ws_id = "collab-%s" % uuid.uuid4().hex[:8]
        ws = CollabWorkspace(
            id=ws_id,
            name=safe_name,
            display_name=chat_title,
            agent_name=safe_name,
            chat_id=chat_id,
            interaction_mode="intelligent",
            status="setup",
            created_by=added_by_id or 0,
            roles={str(added_by_id): "owner"} if added_by_id else {},
        )

        # Register the agent first, write its instructions, then publish the
        # workspace to the routing store. This order closes the race where a
        # message arriving between store.add() and manager.add_agent() would
        # fail to find a registered agent.
        agent = manager.add_agent(
            name=safe_name,
            work_dir=str(WORKSPACE),
            description="[Collab] %s (setup)" % chat_title,
            agent_type="workspace",
            thread_id=None,
        )
        agent.collab_workspace_id = ws_id

        from config import AGENTS_DIR
        agent_file = AGENTS_DIR / ("%s.md" % safe_name)
        try:
            AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            agent_file.write_text(
                "# %s\n\n"
                "Collaborative workspace in setup phase.\n"
                "Ask the user what this workspace should focus on and whether "
                "it should inherit from an existing workspace agent.\n"
                % chat_title
            )
        except OSError as e:
            log.error(
                "Failed to write agent file for collab workspace %s: %s — "
                "rolling back agent registration",
                ws_id, e,
            )
            # Roll back the provisional agent so the manager state is clean.
            try:
                manager.remove_agent(safe_name)
            except Exception:
                log.exception("Rollback remove_agent(%s) also failed", safe_name)
            return

        collab_store.add(ws)
        manager.save_state()

        # Generate invite link
        try:
            link = await platform.get_invite_link(chat_id)
            if link:
                collab_store.update_invite_link(ws_id, link)
        except Exception as e:
            log.warning("Failed to generate invite link for collab %s: %s", ws_id, e)

        # Ask in the group
        try:
            await platform.send_message(
                chat_id=chat_id,
                text="Hi! I've been added to this group. "
                     "How would you like to set up this workspace?\n\n"
                     "- Should I inherit from an existing workspace? If so, which one?\n"
                     "- Or should we start fresh? Tell me what we'll be working on.\n\n"
                     "Once you tell me, I'll configure everything.",
                parse_mode="markdown",
            )
        except Exception as e:
            log.error("Failed to send setup message in group %d: %s", chat_id, e)

        # Also notify in HQ
        try:
            from config import CHAT_ID as hq_chat_id
            await platform.send_message(
                chat_id=hq_chat_id,
                text="I've been added to group *%s* (chat_id: `%d`). "
                     "I'm asking there how to set up the workspace."
                     % (chat_title, chat_id),
                thread_id=platform.control_room_id,
                parse_mode="markdown",
            )
        except Exception as e:
            log.warning("Failed to notify HQ about new group: %s", e)

    def _collab_role(role_str):
        """Coerce a stored role string to a ``Role`` enum, tolerating typos.

        Returns ``Role.PARTICIPANT`` as a safe fallback when ``role_str``
        is not a known value — prevents Flow A from crashing on a
        hand-edited ``collaborative_workspaces.json``. The anomaly is
        logged so the misconfiguration surfaces.
        """
        from collaborative import Role
        try:
            return Role(role_str)
        except ValueError:
            log.warning(
                "Unknown role string %r in collaborative store; falling back to PARTICIPANT",
                role_str,
            )
            return Role.PARTICIPANT

    result = {
        "start": cmd_help,
        "help": cmd_help,
        "workspaces": cmd_workspaces,
        "specialists": cmd_specialists,
        "status": cmd_status,
        "reset": cmd_reset,
        "focus": cmd_focus,
        "ping": cmd_ping,
        "checkupdate": cmd_checkupdate,
        "doupdate": cmd_doupdate,
        "voice": handle_voice,
        "message": handle_message,
    }

    if collab_store is not None:
        result["collab_bot_added"] = collab_bot_added

    return result
