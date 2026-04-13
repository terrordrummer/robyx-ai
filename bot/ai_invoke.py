"""Robyx — AI CLI invocation, response handling, and pattern parsing."""

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from agents import Agent, AgentManager
from ai_backend import AIBackend
from config import (
    AGENTS_DIR,
    AI_TIMEOUT,
    FOCUSED_AGENT_SYSTEM_PROMPT,
    ROBYX_SYSTEM_PROMPT,
    MAX_AI_RETRIES,
    MAX_MESSAGE_LEN,
    SPECIALISTS_DIR,
    WORKSPACE_AGENT_SYSTEM_PROMPT,
)
from i18n import STRINGS
from memory import build_memory_context, get_memory_instructions
from model_preferences import resolve_model_preference

log = logging.getLogger("robyx.invoke")

# ── Response patterns ──
DELEGATION_PATTERN = re.compile(r'\[DELEGATE\s+@(\w+):\s*(.+?)\]', re.DOTALL)
FOCUS_PATTERN = re.compile(r'\[FOCUS\s+@(\w+)\]')
FOCUS_OFF_PATTERN = re.compile(r'\[FOCUS\s+off\]', re.IGNORECASE)

# Workspace creation: [CREATE_WORKSPACE name="x" type="scheduled" frequency="hourly" model="sonnet" scheduled_at="none"]
CREATE_WORKSPACE_PATTERN = re.compile(
    r'\[CREATE_WORKSPACE\s+'
    r'name="([^"]+)"\s+'
    r'type="([^"]+)"\s+'
    r'frequency="([^"]+)"\s+'
    r'model="([^"]+)"\s+'
    r'scheduled_at="([^"]+)"\s*\]',
    re.DOTALL,
)
AGENT_INSTRUCTIONS_PATTERN = re.compile(
    r'\[AGENT_INSTRUCTIONS\](.*?)\[/AGENT_INSTRUCTIONS\]', re.DOTALL
)
CLOSE_WORKSPACE_PATTERN = re.compile(r'\[CLOSE_WORKSPACE\s+name="([^"]+)"\s*\]')

# Continuous task creation
CREATE_CONTINUOUS_PATTERN = re.compile(
    r'\[CREATE_CONTINUOUS\s+'
    r'name="([^"]+)"\s+'
    r'work_dir="([^"]+)"\s*\]',
    re.DOTALL,
)
CONTINUOUS_PROGRAM_PATTERN = re.compile(
    r'\[CONTINUOUS_PROGRAM\](.*?)\[/CONTINUOUS_PROGRAM\]', re.DOTALL
)

# Specialist creation
CREATE_SPECIALIST_PATTERN = re.compile(
    r'\[CREATE_SPECIALIST\s+name="([^"]+)"\s+model="([^"]+)"\s*\]'
)
SPECIALIST_INSTRUCTIONS_PATTERN = re.compile(
    r'\[SPECIALIST_INSTRUCTIONS\](.*?)\[/SPECIALIST_INSTRUCTIONS\]', re.DOTALL
)

# Cross-workspace specialist request
REQUEST_PATTERN = re.compile(r'\[REQUEST\s+@(\w+):\s*(.+?)\]', re.DOTALL)

# Service restart request
RESTART_PATTERN = re.compile(r'\[RESTART\]')

# Real-time progress updates
STATUS_PATTERN = re.compile(r'\[STATUS\s+(.+?)\]')

# Outgoing image: [SEND_IMAGE path="/abs/path.png" caption="optional text"]
# Agents must emit this only on explicit user request — never proactively.
# The `caption` attribute is optional.
SEND_IMAGE_PATTERN = re.compile(
    r'\[SEND_IMAGE\s+path="([^"]+)"(?:\s+caption="([^"]*)")?\s*\]'
)

# Schedule a reminder: [REMIND at="2026-04-08T17:32:00+02:00" text="..."]
# or [REMIND in="2m" text="..."] or [REMIND in="1h30m" text="..." thread="903"]
# Attributes can appear in any order. The bot defaults `thread` to the agent's
# own topic so agents never need to know their own id.
REMIND_PATTERN = re.compile(r'\[REMIND\s+([^\]]+?)\s*\]')
_REMIND_ATTR_PATTERN = re.compile(r'(\w+)="([^"]*)"')
_REMIND_DURATION_PATTERN = re.compile(
    r'^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$'
)


def parse_remind_attrs(blob: str) -> dict:
    """Parse the inner attribute string of a [REMIND ...] match into a dict."""
    return dict(_REMIND_ATTR_PATTERN.findall(blob))


def parse_remind_when(
    at: str | None, in_: str | None, now: datetime | None = None
) -> datetime:
    """Resolve a REMIND ``at=`` / ``in=`` value to an aware UTC datetime.

    Exactly one of ``at`` or ``in_`` must be supplied. ``at`` is an ISO-8601
    string with an explicit timezone. ``in_`` is a compact duration like
    ``90s``, ``2m``, ``1h30m``, ``2d``. Past ``at`` values are tolerated up
    to 60 s before ``now`` (clock skew); anything older is rejected. ``in_``
    durations must be positive and at most 90 days.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if (at and in_) or (not at and not in_):
        raise ValueError("REMIND requires exactly one of at= or in=")
    if at:
        try:
            dt = datetime.fromisoformat(at)
        except ValueError as e:
            raise ValueError("invalid at= datetime: %s" % at) from e
        if dt.tzinfo is None:
            raise ValueError("at= must include a timezone offset")
        dt_utc = dt.astimezone(timezone.utc)
        if dt_utc < now - timedelta(seconds=60):
            raise ValueError("at= is in the past")
        return dt_utc
    # in_
    m = _REMIND_DURATION_PATTERN.match(in_.strip())
    if not m or not any(m.groups()):
        raise ValueError("invalid in= duration: %s" % in_)
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if delta.total_seconds() <= 0:
        raise ValueError("in= duration must be positive")
    if delta.days > 90:
        raise ValueError("in= duration exceeds 90 days")
    return now + delta


RATE_LIMIT_KEYWORDS = [
    "rate limit", "limit reached", "hit your limit", "too many requests",
    "usage cap", "over capacity", "quota exceeded", "throttl",
]


def _normalize_backend_response(parsed_response):
    """Normalize a backend's parse_response() result into ``(text, session_id)``.

    Backends may return either a plain string (legacy) or a dict with at
    least a ``text`` key plus an optional ``session_id``. This helper hides
    the difference from the rest of the invocation pipeline.
    """
    if isinstance(parsed_response, dict):
        text = parsed_response.get("text", "") or ""
        session_id = parsed_response.get("session_id")
        return text, session_id
    return parsed_response or "", None


def _load_agent_instructions(agent: Agent) -> str:
    """Return the workspace/specialist markdown instructions, ready to append.

    The orchestrator's system prompt covers behaviour common to *all*
    workspace and specialist agents, but each individual agent also has its
    own per-agent instructions in ``agents/<name>.md`` /
    ``specialists/<name>.md``. This loader injects them into the system
    prompt at invocation time so interactive turns honour the same brief
    that scheduled runs already see.
    """
    if agent.agent_type == "workspace":
        path = AGENTS_DIR / (agent.name + ".md")
    elif agent.agent_type == "specialist":
        path = SPECIALISTS_DIR / (agent.name + ".md")
    else:
        return ""

    if not path.exists():
        return ""

    instructions = path.read_text().strip()
    if not instructions:
        return ""

    return "\n\n## Agent Instructions\n" + instructions


def _agent_model_role(agent: Agent) -> str:
    """Map an agent to the role key used by ``models.yaml`` defaults."""
    if agent.name == "robyx":
        return "orchestrator"
    if agent.agent_type == "specialist":
        return "specialist"
    return "workspace"


def _is_rate_limited(text: str) -> bool:
    return any(kw in text for kw in RATE_LIMIT_KEYWORDS)


def _classify_error(combined: str, err: str, out: str) -> str:
    if _is_rate_limited(combined):
        return STRINGS["rate_limited"]
    if "network" in combined or "connection" in combined or "timeout" in combined:
        return STRINGS["network_error"]
    if "permission" in combined or "denied" in combined:
        return STRINGS["permission_denied"]
    if "session" in combined and ("not found" in combined or "invalid" in combined):
        return STRINGS["session_expired"]
    detail = err or out or "unknown"
    return STRINGS["ai_error"] % detail[:300]


async def _write_stdin_payload(proc, payload: bytes | None) -> None:
    """Write an optional stdin payload to a spawned subprocess."""
    if payload is None or proc.stdin is None:
        return

    write_result = proc.stdin.write(payload)
    if inspect.isawaitable(write_result):
        await write_result
    await proc.stdin.drain()
    close_result = proc.stdin.close()
    if inspect.isawaitable(close_result):
        await close_result
    wait_closed = getattr(proc.stdin, "wait_closed", None)
    if callable(wait_closed):
        await wait_closed()


async def invoke_ai(
    agent: Agent,
    message: str,
    chat_id: int,
    platform,
    manager: AgentManager,
    backend: AIBackend,
    is_orchestrator_call: bool = False,
    model: str | None = None,
    _retry: int = 0,
    thread_id: int | None = None,
) -> str:
    """Invoke the AI CLI with session persistence, keep-alive, and per-agent locking.

    *model* may be a semantic alias (``fast``/``balanced``/``powerful``), an
    explicit backend model id, or ``None`` (in which case the agent's own
    preference and the role default from ``models.yaml`` are consulted).

    If the agent is currently busy with a running subprocess, the running
    process is interrupted (SIGTERM → SIGKILL) so the user's message is
    processed immediately instead of queuing behind the lock.
    """
    # Interrupt running subprocess if the agent is busy — the user's new
    # message takes priority over the in-flight task.
    if agent.busy and agent.running_proc is not None:
        log.info(
            "Interrupting agent [%s] (PID %d) for user message",
            agent.name, agent.running_proc.pid,
        )
        await agent.interrupt()

    async with agent.lock:
        return await _invoke_ai_locked(
            agent, message, chat_id, platform, manager, backend,
            is_orchestrator_call, model, _retry, thread_id,
        )


async def _invoke_ai_locked(
    agent, message, chat_id, platform, manager, backend,
    is_orchestrator_call, model, _retry, thread_id,
):
    # Resolve the actual model id to pass to the backend. Caller-provided
    # value wins; otherwise we use the agent's stored preference; otherwise
    # the role default from models.yaml.
    role = _agent_model_role(agent)
    effective_model = resolve_model_preference(model or agent.model, backend, role=role)

    # Determine system prompt
    system_prompt = None
    if agent.name == "robyx":
        system_prompt = ROBYX_SYSTEM_PROMPT
    elif manager.focused_agent == agent.name:
        system_prompt = FOCUSED_AGENT_SYSTEM_PROMPT
    elif agent.agent_type in ("workspace", "specialist"):
        system_prompt = WORKSPACE_AGENT_SYSTEM_PROMPT
        # Append the per-agent markdown brief so interactive turns get the
        # same instructions the scheduled runs already use.
        system_prompt = system_prompt + _load_agent_instructions(agent)

    # Inject memory context and management instructions
    if system_prompt:
        memory_ctx = build_memory_context(agent.name, agent.agent_type, agent.work_dir)
        memory_instr = get_memory_instructions(agent.name, agent.agent_type, agent.work_dir)
        system_prompt = system_prompt + memory_ctx + "\n\n" + memory_instr

        # Warn when the assembled system prompt is unusually large. A rough
        # word-to-token ratio of 1.3 gives a conservative estimate; the hard
        # ceiling depends on the model but 30 000 words (~40k tokens) already
        # eats a significant chunk of any context window.
        _prompt_words = len(system_prompt.split())
        if _prompt_words > 30_000:
            log.warning(
                "System prompt for %s is very large (%d words, ~%dk tokens). "
                "Consider trimming agent instructions or archiving memory.",
                agent.name, _prompt_words, int(_prompt_words * 1.3 / 1000),
            )

    # Build command. Only reuse a stored session id if the backend can
    # actually consume it — Robyx stores a UUID per agent for its own
    # bookkeeping, but some backends (e.g. OpenCode) require their own id
    # format and would reject ours.
    session_id = None
    if backend.supports_sessions() and backend.can_resume_session(agent.session_id):
        session_id = agent.session_id
    is_resume = bool(session_id and (agent.session_started or agent.message_count > 0))

    cmd = backend.build_command(
        message=message,
        session_id=session_id,
        system_prompt=system_prompt,
        model=effective_model,
        work_dir=agent.work_dir,
        is_resume=is_resume,
    )
    stdin_payload = backend.command_stdin_payload(message)

    log.info(
        "Invoking %s for [%s] with model %s (chars=%d)",
        backend.name, agent.name, effective_model, len(message or ""),
    )
    agent.busy = True

    stop_keepalive = asyncio.Event()

    effective_thread_id = thread_id

    async def keepalive_loop():
        try:
            await platform.send_typing(chat_id, effective_thread_id)
        except Exception as e:
            log.warning("Keep-alive initial error: %s", e)
        while not stop_keepalive.is_set():
            await asyncio.sleep(5)
            if stop_keepalive.is_set():
                break
            try:
                await platform.send_typing(chat_id, effective_thread_id)
            except Exception as e:
                log.warning("Keep-alive error: %s", e)

    keepalive_task = asyncio.create_task(keepalive_loop())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent.work_dir,
            limit=1024 * 1024,
        )
        agent.running_proc = proc

        backend_session_id: str | None = None
        if backend.supports_streaming():
            await _write_stdin_payload(proc, stdin_payload)
            text = await _read_stream(proc, platform, chat_id, effective_thread_id, backend)
        else:
            if stdin_payload is not None:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_payload),
                    timeout=AI_TIMEOUT,
                )
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AI_TIMEOUT)
            out = stdout.decode().strip()
            err = stderr.decode().strip()
            combined = (out + " " + err).lower()

            if proc.returncode != 0:
                if agent.interrupted:
                    log.info("Agent [%s] interrupted by user", agent.name)
                    return None
                log.error(
                    "%s error for [%s] (rc=%d, stderr_len=%d, stdout_len=%d)",
                    backend.name, agent.name, proc.returncode, len(err), len(out),
                )
                if "already in use" in combined and _retry < MAX_AI_RETRIES:
                    new_sid = str(uuid.uuid4())
                    log.warning(
                        "Session collision for [%s]: id=%s → regenerating as %s, retry %d/%d",
                        agent.name, agent.session_id, new_sid, _retry + 1, MAX_AI_RETRIES,
                    )
                    agent.session_id = new_sid
                    agent.session_started = False
                    agent.message_count = 0
                    manager.save_state()
                    stop_keepalive.set()
                    keepalive_task.cancel()
                    agent.busy = False
                    await asyncio.sleep(min(2 ** _retry, 16))
                    return await _invoke_ai_locked(
                        agent, message, chat_id, platform, manager, backend,
                        is_orchestrator_call, model, _retry + 1, thread_id,
                    )
                return _classify_error(combined, err, out)

            if not out:
                return STRINGS["ai_no_response"]

            parsed_response = backend.parse_response(out, proc.returncode)
            text, backend_session_id = _normalize_backend_response(parsed_response)

        # Handle streaming errors (returned as None)
        if text is None:
            if agent.interrupted:
                log.info("Agent [%s] interrupted by user (streaming)", agent.name)
                return None
            stderr_data = await proc.stderr.read()
            err = stderr_data.decode().strip() if stderr_data else ""
            combined = err.lower()
            if proc.returncode != 0:
                log.error(
                    "%s error for [%s] (rc=%d, stderr_len=%d)",
                    backend.name, agent.name, proc.returncode, len(err),
                )
                if "already in use" in combined and _retry < MAX_AI_RETRIES:
                    new_sid = str(uuid.uuid4())
                    log.warning(
                        "Session collision for [%s]: id=%s → regenerating as %s, retry %d/%d",
                        agent.name, agent.session_id, new_sid, _retry + 1, MAX_AI_RETRIES,
                    )
                    agent.session_id = new_sid
                    agent.session_started = False
                    agent.message_count = 0
                    manager.save_state()
                    stop_keepalive.set()
                    keepalive_task.cancel()
                    agent.busy = False
                    await asyncio.sleep(min(2 ** _retry, 16))
                    return await _invoke_ai_locked(
                        agent, message, chat_id, platform, manager, backend,
                        is_orchestrator_call, model, _retry + 1, thread_id,
                    )
                return _classify_error(combined, err, "")
            return STRINGS["ai_no_response"]

        if not text:
            return STRINGS["ai_empty"]
        if _is_rate_limited(text.lower()):
            return STRINGS["rate_limited"]

        # If the backend handed us a native session id (e.g. OpenCode's
        # ``ses_…``), persist it so the next turn can resume the
        # conversation server-side.
        if backend_session_id and backend_session_id != agent.session_id:
            agent.session_id = backend_session_id

        agent.last_used = time.time()
        agent.message_count += 1
        agent.session_started = True
        manager.save_state()
        return text

    except asyncio.TimeoutError:
        log.error("AI timeout for [%s]", agent.name)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return STRINGS["ai_timeout"] % AI_TIMEOUT
    except (OSError, RuntimeError) as e:
        log.error("AI exception for [%s]: %s", agent.name, e, exc_info=True)
        return STRINGS["ai_error"] % str(e)
    finally:
        agent.busy = False
        agent.interrupted = False
        agent.running_proc = None
        stop_keepalive.set()
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


async def _read_stream(proc, platform, chat_id, effective_thread_id, backend):
    """Read stream-json stdout line by line, relay [STATUS ...] in real time.

    Returns the final result text (with STATUS patterns stripped),
    or None if no result event was found.
    """
    result_text = None
    sent_statuses = set()

    async def _send_status(msg):
        if msg in sent_statuses:
            return
        sent_statuses.add(msg)
        try:
            await platform.send_message(
                chat_id=chat_id,
                text=msg,
                thread_id=effective_thread_id,
                parse_mode="markdown",
            )
        except Exception as e:
            log.warning("Failed to send status update: %s", e)

    deadline = asyncio.get_event_loop().time() + AI_TIMEOUT

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            proc.kill()
            raise asyncio.TimeoutError()

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            proc.kill()
            raise

        if not line:
            break

        line_str = line.decode().strip()
        if not line_str:
            continue

        try:
            event = json.loads(line_str)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        # Relay STATUS patterns from assistant text blocks in real time
        if event_type == "assistant":
            msg = event.get("message", {})
            content_blocks = msg.get("content", []) if isinstance(msg, dict) else []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    for match in STATUS_PATTERN.finditer(text):
                        await _send_status("_%s_" % match.group(1).strip())

        elif event_type == "result":
            result_text = event.get("result", "") or ""

    await proc.wait()

    if result_text is None:
        return None

    # Strip STATUS patterns from final response
    result_text = STATUS_PATTERN.sub("", result_text).strip()
    result_text = re.sub(r'\n{3,}', '\n\n', result_text)
    return result_text


# ── Response pattern handlers ──

async def handle_delegations(response, chat_id, platform, manager, backend, thread_id=None):
    """Parse orchestrator response for [DELEGATE @agent: task] patterns."""
    delegations = list(DELEGATION_PATTERN.finditer(response))
    if not delegations:
        return response

    clean_response = DELEGATION_PATTERN.sub("", response).strip()
    results = []

    for match in delegations:
        target_name = match.group(1).lower()
        task = match.group(2).strip()

        target = manager.get(target_name)
        if not target:
            results.append(STRINGS["delegation_agent_missing"] % target_name)
            continue

        log.info("Delegation: robyx -> %s: %s...", target_name, task[:80])
        try:
            await platform.send_message(
                chat_id=chat_id,
                text=STRINGS["delegation_sent"] % (target_name, task[:200]),
                thread_id=thread_id,
                parse_mode="markdown",
            )
        except Exception:
            pass

        delegate_response = await invoke_ai(
            target, task, chat_id, platform, manager, backend, thread_id=thread_id,
        )
        manager.save_state()
        if delegate_response is not None:
            results.append(STRINGS["delegation_result"] % (target_name, delegate_response))

    parts = [p for p in [clean_response] + results if p]
    return "\n\n---\n\n".join(parts)


async def handle_focus_commands(response, chat_id, platform, manager, thread_id=None):
    """Parse response for [FOCUS @agent] or [FOCUS off]."""
    if FOCUS_OFF_PATTERN.search(response):
        old_focus = manager.focused_agent
        manager.clear_focus()
        clean = FOCUS_OFF_PATTERN.sub("", response).strip()
        log.info("Focus OFF (was: %s)", old_focus)
        try:
            await platform.send_message(
                chat_id=chat_id,
                text=STRINGS["focus_off"],
                thread_id=thread_id,
                parse_mode="markdown",
            )
        except Exception:
            pass
        return clean

    focus_match = FOCUS_PATTERN.search(response)
    if focus_match:
        target_name = focus_match.group(1).lower()
        clean = FOCUS_PATTERN.sub("", response).strip()

        target = manager.get(target_name)
        if target:
            manager.set_focus(target_name)
            log.info("Focus ON: %s", target_name)
            try:
                await platform.send_message(
                    chat_id=chat_id,
                    text=STRINGS["focus_on"] % (target_name, target_name),
                    thread_id=thread_id,
                    parse_mode="markdown",
                )
            except Exception:
                pass
        else:
            try:
                await platform.send_message(
                    chat_id=chat_id,
                    text=STRINGS["agent_not_found"] % target_name,
                    thread_id=thread_id,
                    parse_mode="markdown",
                )
            except Exception:
                pass
        return clean

    return response


async def handle_specialist_requests(response, chat_id, platform, manager, backend, requesting_agent, thread_id=None):
    """Parse workspace agent response for [REQUEST @specialist: task] patterns."""
    requests = list(REQUEST_PATTERN.finditer(response))
    if not requests:
        return response

    clean_response = REQUEST_PATTERN.sub("", response).strip()
    results = []

    for match in requests:
        specialist_name = match.group(1).lower()
        task = match.group(2).strip()

        specialist = manager.get(specialist_name)
        if not specialist or specialist.agent_type != "specialist":
            results.append("Specialist *%s* not found." % specialist_name)
            continue

        log.info("Specialist request: %s -> %s: %s...", requesting_agent.name, specialist_name, task[:80])
        try:
            await platform.send_message(
                chat_id=chat_id,
                text="Requesting *%s*: _%s_" % (specialist_name, task[:200]),
                thread_id=thread_id,
                parse_mode="markdown",
            )
        except Exception:
            pass

        contextualized_task = "Request from workspace '%s': %s" % (requesting_agent.name, task)
        specialist_response = await invoke_ai(
            specialist, contextualized_task, chat_id, platform, manager, backend,
            thread_id=thread_id,
        )
        manager.save_state()
        if specialist_response is not None:
            results.append("*%s*:\n%s" % (specialist_name, specialist_response))

    parts = [p for p in [clean_response] + results if p]
    return "\n\n---\n\n".join(parts)


def split_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks
