"""Robyx — Agent model and session manager."""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from config import STATE_FILE, WORKSPACE
from i18n import STRINGS
from persistence_recovery import guard_json_write, load_json_with_recovery

log = logging.getLogger("robyx.agents")


def _valid_agent_state(value: Any) -> bool:
    """Validate the whole registry before committing any entry in memory."""
    if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
        return False
    focused = value.get("focused_agent")
    if focused is not None and not isinstance(focused, str):
        return False
    for name, payload in value["agents"].items():
        if not isinstance(name, str) or not name or not isinstance(payload, dict):
            return False
        # The orchestrator's historical record only contained session fields.
        if name != "robyx":
            if payload.get("name") != name:
                return False
            if not isinstance(payload.get("work_dir"), str):
                return False
            if not isinstance(payload.get("description"), str):
                return False
        for key in ("session_id", "agent_type", "model", "backend", "collab_workspace_id"):
            item = payload.get(key)
            if item is not None and not isinstance(item, str):
                return False
        for key in ("created_at", "last_used"):
            item = payload.get(key)
            if item is not None and (
                not isinstance(item, (int, float)) or isinstance(item, bool)
            ):
                return False
        count = payload.get("message_count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
            return False
        started = payload.get("session_started")
        if started is not None and not isinstance(started, bool):
            return False
    return True


def _is_placeholder_session_id(sid: str) -> bool:
    """Return True for obviously-bad session ids we must not reuse.

    We treat as placeholder:
    - empty / missing values
    - the sequential ``00000000-0000-0000-0000-0000000000XX`` family that
      leaked into early state files (they deterministically collide in the
      Claude CLI session registry, causing "Session ID already in use" errors
      that cannot be recovered by retrying with the same id)
    - any string that does not parse as a valid UUID
    """
    if not sid:
        return True
    if sid.startswith("00000000-0000-0000-0000-"):
        return True
    try:
        uuid.UUID(sid)
    except (ValueError, AttributeError, TypeError):
        return True
    return False


@dataclass
class Agent:
    name: str
    work_dir: str
    description: str
    agent_type: str = "workspace"  # workspace, specialist, orchestrator
    model: str | None = None  # semantic alias or explicit backend model id
    backend: str | None = None  # per-agent backend override ("claude", "codex", "opencode"); None ⇒ global default
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    message_count: int = 0
    session_started: bool = False
    thread_id: Any = None
    collab_workspace_id: str | None = None
    busy: bool = False
    interrupted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    running_proc: Any = field(default=None, repr=False, compare=False)
    running_profile: str | None = field(default=None, repr=False, compare=False)

    async def interrupt(self) -> bool:
        """Interrupt and reap the supervised process tree.

        State is cleared only after the supervisor confirms that the complete
        tree stopped.  A failed termination keeps the process/busy evidence so
        callers cannot mistake a surviving child for an idle agent.
        """
        proc = self.running_proc
        if proc is None:
            return False
        self.interrupted = True

        from runtime_supervisor import get_runtime_supervisor
        stopped = await get_runtime_supervisor().terminate_process(
            proc,
            grace_seconds=5.0,
        )
        if stopped:
            self.running_proc = None
            self.busy = False
            self.running_profile = None
        return stopped

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "work_dir": self.work_dir,
            "description": self.description,
            "agent_type": self.agent_type,
            "model": self.model,
            "backend": self.backend,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "message_count": self.message_count,
            "session_started": self.session_started,
            "thread_id": self.thread_id,
            "collab_workspace_id": self.collab_workspace_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Agent":
        known = {f for f in cls.__dataclass_fields__} - {
            "lock", "busy", "running_proc", "running_profile",
        }
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


class AgentManager:
    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self.focused_agent: Optional[str] = None
        self._topic_map: dict[Any, str] = {}  # channel/thread id → agent name
        self._agents_lock = asyncio.Lock()
        self._setup_orchestrator()
        self._load_state()

    def _setup_orchestrator(self):
        self.agents["robyx"] = Agent(
            name="robyx",
            work_dir=str(WORKSPACE),
            description="Principal Orchestrator — manages all workspaces and agents",
            agent_type="orchestrator",
            thread_id=1,  # General / Main topic
        )

    def _load_state(self):
        """Read ``STATE_FILE`` into ``self.agents``.

        Called only from ``__init__``. Cross-process safety of the
        subsequent save cycle relies on
        :func:`bot.bot.ensure_single_instance` — do NOT invoke this method
        mid-run from another call site without adding explicit file
        locking around the load.
        """
        result = load_json_with_recovery(
            STATE_FILE,
            data_dir=STATE_FILE.parent,
            validator=_valid_agent_state,
            kind="agent state",
            logger=log,
        )
        if result.status == "missing":
            return

        data = result.value
        loaded_agents = {
            "robyx": Agent.from_dict(self.agents["robyx"].to_dict()),
        }
        dirty = False
        for name, agent_data in data["agents"].items():
            if name == "robyx":
                sid = agent_data.get("session_id", loaded_agents[name].session_id)
                if _is_placeholder_session_id(sid):
                    log.warning(
                        "Sanitising placeholder session_id for [%s]: %s", name, sid,
                    )
                    sid = str(uuid.uuid4())
                    dirty = True
                loaded_agents[name].session_id = sid
                loaded_agents[name].message_count = agent_data.get("message_count", 0)
                loaded_agents[name].session_started = agent_data.get("session_started", False)
            else:
                agent = Agent.from_dict(agent_data)
                if _is_placeholder_session_id(agent.session_id):
                    log.warning(
                        "Sanitising placeholder session_id for [%s]: %s",
                        name, agent.session_id,
                    )
                    agent.session_id = str(uuid.uuid4())
                    agent.session_started = False
                    agent.message_count = 0
                    dirty = True
                loaded_agents[name] = agent

        # Commit only after the complete document has validated and parsed.
        self.agents = loaded_agents
        self.focused_agent = data.get("focused_agent")
        self._rebuild_topic_map()
        log.info("Loaded state: %s (focus: %s)", list(self.agents.keys()), self.focused_agent)
        if dirty:
            self.save_state()

    def _rebuild_topic_map(self):
        """Rebuild thread_id → agent name mapping from current agents."""
        self._topic_map = {}
        for agent in self.agents.values():
            if agent.thread_id and agent.name != "robyx":
                self._topic_map[agent.thread_id] = agent.name

    def _guard_state_write(self) -> None:
        guard_json_write(
            STATE_FILE,
            data_dir=STATE_FILE.parent,
            validator=_valid_agent_state,
            kind="agent state",
            logger=log,
        )

    def save_state(self):
        self._guard_state_write()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": {n: a.to_dict() for n, a in self.agents.items()},
            "focused_agent": self.focused_agent,
        }
        if not _valid_agent_state(data):
            raise ValueError("refusing to persist malformed agent state")
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, STATE_FILE)

    async def async_save_state(self):
        """Save state under the agents lock for concurrent-safe writes."""
        async with self._agents_lock:
            self.save_state()

    async def reload_state_after_maintenance_restore(self) -> None:
        """Replace live state with the authoritative restored data tree.

        The updater calls this while holding the process-wide exclusive
        maintenance lease, after restoring a pre-update snapshot.  Replacing
        the complete in-memory graph prevents a later ``save_state()`` from
        reintroducing state written by a failed target migration.
        """
        async with self._agents_lock:
            self.agents = {}
            self.focused_agent = None
            self._topic_map = {}
            self._setup_orchestrator()
            self._load_state()

    def reset_sessions(self, agent_names: set[str] | None = None) -> list[str]:
        """Regenerate AI-CLI sessions for the given agents (or all if ``None``).

        This is the **only** correct way to invalidate agent sessions while
        the bot is running. Earlier versions of Robyx mutated
        ``data/state.json`` directly from the migration framework or the
        updater; that worked in unit tests against a dict but was silently
        clobbered in production by the next ``save_state()`` call, because
        the live :class:`AgentManager` held the pre-mutation copy in
        memory and rewrote it on every interaction. This method mutates
        ``self.agents`` in place and immediately persists, so the live
        copy and the file on disk are always in sync.

        For each affected agent we follow the same convention
        :meth:`_load_state` already uses for placeholder UUID
        sanitisation: a fresh ``uuid.uuid4()``, ``session_started=False``,
        ``message_count=0``. Every other field of every agent is left
        verbatim — ``thread_id``, ``name``, ``work_dir``, ``model``,
        ``description``, ``created_at``, ``last_used``, ``busy``.

        Args:
          agent_names: the set of names to reset. ``None`` (default)
            resets every known agent. Names not present in
            ``self.agents`` are silently ignored, which protects renames
            and removals when the caller is the diff-driven updater.

        Returns:
          The sorted list of agent names that were actually reset.
          Possibly empty.
        """
        if agent_names is None:
            target_names = list(self.agents.keys())
        else:
            target_names = [n for n in agent_names if n in self.agents]

        if not target_names:
            return []

        self._guard_state_write()
        before = {
            name: (
                self.agents[name].session_id,
                self.agents[name].session_started,
                self.agents[name].message_count,
            )
            for name in target_names
        }
        for name in target_names:
            agent = self.agents[name]
            agent.session_id = str(uuid.uuid4())
            agent.session_started = False
            agent.message_count = 0
        try:
            self.save_state()
        except BaseException:
            for name, values in before.items():
                agent = self.agents[name]
                agent.session_id, agent.session_started, agent.message_count = values
            raise
        log.info(
            "AgentManager.reset_sessions: regenerated AI-CLI sessions for %d agent(s): %s",
            len(target_names), ", ".join(sorted(target_names)),
        )
        return sorted(target_names)

    async def async_add_agent(
        self,
        name: str,
        work_dir: str,
        description: str,
        agent_type: str = "workspace",
        model: str | None = None,
        thread_id: Any = None,
        backend: str | None = None,
    ) -> Agent:
        """Concurrent-safe variant of :meth:`add_agent`."""
        async with self._agents_lock:
            return self.add_agent(
                name, work_dir, description, agent_type, model, thread_id, backend,
            )

    def add_agent(
        self,
        name: str,
        work_dir: str,
        description: str,
        agent_type: str = "workspace",
        model: str | None = None,
        thread_id: Any = None,
        backend: str | None = None,
    ) -> Agent:
        """Add or update an agent.

        ``model`` is the semantic alias (``fast``/``balanced``/``powerful``) or
        explicit backend model id the agent should run with by default.
        Resolved to a concrete model id at invocation time by
        :func:`bot.model_preferences.resolve_model_preference`.

        ``backend`` overrides the global ``AI_BACKEND`` for this agent only
        (``"claude"`` / ``"codex"`` / ``"opencode"``). ``None`` keeps the
        installation-wide default — that is the normal case. Setting it lets
        a single workspace run on a different CLI without touching the rest
        of the fleet (e.g. a Codex-driven workspace alongside Claude-driven
        ones).

        For concurrent-safe usage from async code, prefer :meth:`async_add_agent`.
        """
        self._guard_state_write()
        existing = self.agents.get(name)
        before = None
        if existing is not None:
            before = (
                existing.work_dir,
                existing.thread_id,
                existing.description,
                existing.model,
                existing.backend,
            )
        if name in self.agents:
            agent = self.agents[name]
            if work_dir:
                agent.work_dir = work_dir
            agent.thread_id = thread_id or agent.thread_id
            agent.description = description or agent.description
            if model:
                agent.model = model
            if backend:
                agent.backend = backend
        else:
            agent = Agent(
                name=name,
                work_dir=work_dir,
                description=description,
                agent_type=agent_type,
                model=model,
                backend=backend,
                thread_id=thread_id,
            )
            self.agents[name] = agent
        self._rebuild_topic_map()
        try:
            self.save_state()
        except BaseException:
            if existing is None:
                self.agents.pop(name, None)
            else:
                (
                    existing.work_dir,
                    existing.thread_id,
                    existing.description,
                    existing.model,
                    existing.backend,
                ) = before
            self._rebuild_topic_map()
            raise
        return agent

    async def async_remove_agent(self, name: str) -> bool:
        """Concurrent-safe variant of :meth:`remove_agent`."""
        async with self._agents_lock:
            return self.remove_agent(name)

    def remove_agent(self, name: str) -> bool:
        if name in self.agents and name != "robyx":
            self._guard_state_write()
            removed = self.agents[name]
            previous_focus = self.focused_agent
            if self.focused_agent == name:
                self.focused_agent = None
            del self.agents[name]
            self._rebuild_topic_map()
            try:
                self.save_state()
            except BaseException:
                self.agents[name] = removed
                self.focused_agent = previous_focus
                self._rebuild_topic_map()
                raise
            return True
        return False

    def get(self, name: str) -> Optional[Agent]:
        return self.agents.get(name)

    def get_by_thread(self, thread_id: Any) -> Optional[Agent]:
        """Get agent by the platform-specific channel/thread identifier."""
        name = self._topic_map.get(thread_id)
        return self.agents.get(name) if name else None

    def list_active(self) -> list[Agent]:
        return list(self.agents.values())

    def list_workspaces(self) -> list[Agent]:
        return [a for a in self.agents.values() if a.agent_type == "workspace"]

    def list_specialists(self) -> list[Agent]:
        return [a for a in self.agents.values() if a.agent_type == "specialist"]

    def find_by_mention(self, text: str) -> Optional[Agent]:
        for word in text.split():
            if word.startswith("@"):
                name = word[1:].lower().strip(".,!?")
                if name in self.agents:
                    return self.agents[name]
        return None

    def set_focus(self, name: str) -> bool:
        if name in self.agents:
            self._guard_state_write()
            previous = self.focused_agent
            self.focused_agent = name
            try:
                self.save_state()
            except BaseException:
                self.focused_agent = previous
                raise
            return True
        return False

    def clear_focus(self):
        self._guard_state_write()
        previous = self.focused_agent
        self.focused_agent = None
        try:
            self.save_state()
        except BaseException:
            self.focused_agent = previous
            raise

    def resolve_agent(self, text: str) -> tuple[Agent, str]:
        """Determine target agent: explicit @mention > focus > robyx."""
        target = self.find_by_mention(text)
        if target:
            clean_text = text
            for word in text.split():
                if word.startswith("@") and word[1:].lower().strip(".,!?") == target.name:
                    clean_text = text.replace(word, "").strip()
                    break
            return target, clean_text

        if self.focused_agent:
            focused = self.get(self.focused_agent)
            if focused:
                return focused, text

        return self.get("robyx"), text

    def get_status_summary(self) -> str:
        lines = []
        for a in self.agents.values():
            if a.name == "robyx":
                continue
            icon = "..." if a.busy else "o"
            age = format_age(a.last_used)
            focus = " *" if self.focused_agent == a.name else ""
            tag = "[S]" if a.agent_type == "specialist" else "[W]"
            lines.append(
                "%s %s *%s*%s — %s (last: %s)" % (icon, tag, a.name, focus, a.description, age)
            )
        if not lines:
            return STRINGS["no_agents"]
        return "\n".join(lines)


def format_age(timestamp: float) -> str:
    delta = time.time() - timestamp
    if delta < 60:
        return STRINGS["time_now"]
    if delta < 3600:
        return STRINGS["time_minutes"] % int(delta / 60)
    if delta < 86400:
        return STRINGS["time_hours"] % int(delta / 3600)
    return STRINGS["time_days"] % int(delta / 86400)
