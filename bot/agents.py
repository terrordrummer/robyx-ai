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

log = logging.getLogger("robyx.agents")


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

    async def interrupt(self) -> bool:
        """Interrupt the running subprocess. SIGTERM with 5s grace, then SIGKILL.

        Returns True if a process was actually interrupted.
        """
        proc = self.running_proc
        if proc is None:
            return False
        self.interrupted = True
        try:
            proc.terminate()  # SIGTERM
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()  # SIGKILL
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            return True
        except ProcessLookupError:
            return False
        finally:
            self.running_proc = None
            self.busy = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "work_dir": self.work_dir,
            "description": self.description,
            "agent_type": self.agent_type,
            "model": self.model,
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
        known = {f for f in cls.__dataclass_fields__} - {"lock", "busy", "running_proc"}
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
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            dirty = False
            for name, agent_data in data.get("agents", {}).items():
                if name == "robyx":
                    sid = agent_data.get("session_id", self.agents[name].session_id)
                    if _is_placeholder_session_id(sid):
                        log.warning(
                            "Sanitising placeholder session_id for [%s]: %s", name, sid,
                        )
                        sid = str(uuid.uuid4())
                        dirty = True
                    self.agents[name].session_id = sid
                    self.agents[name].message_count = agent_data.get("message_count", 0)
                    self.agents[name].session_started = agent_data.get("session_started", False)
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
                    self.agents[name] = agent
            self.focused_agent = data.get("focused_agent")
            self._rebuild_topic_map()
            log.info("Loaded state: %s (focus: %s)", list(self.agents.keys()), self.focused_agent)
            if dirty:
                # Persist the sanitised IDs so the next run doesn't re-sanitise.
                self.save_state()
        except Exception as e:
            log.warning("Failed to load state: %s", e)

    def _rebuild_topic_map(self):
        """Rebuild thread_id → agent name mapping from current agents."""
        self._topic_map = {}
        for agent in self.agents.values():
            if agent.thread_id and agent.name != "robyx":
                self._topic_map[agent.thread_id] = agent.name

    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": {n: a.to_dict() for n, a in self.agents.items()},
            "focused_agent": self.focused_agent,
        }
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, STATE_FILE)

    async def async_save_state(self):
        """Save state under the agents lock for concurrent-safe writes."""
        async with self._agents_lock:
            self.save_state()

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

        for name in target_names:
            agent = self.agents[name]
            agent.session_id = str(uuid.uuid4())
            agent.session_started = False
            agent.message_count = 0
        self.save_state()
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
    ) -> Agent:
        """Concurrent-safe variant of :meth:`add_agent`."""
        async with self._agents_lock:
            return self.add_agent(name, work_dir, description, agent_type, model, thread_id)

    def add_agent(
        self,
        name: str,
        work_dir: str,
        description: str,
        agent_type: str = "workspace",
        model: str | None = None,
        thread_id: Any = None,
    ) -> Agent:
        """Add or update an agent.

        ``model`` is the semantic alias (``fast``/``balanced``/``powerful``) or
        explicit backend model id the agent should run with by default.
        Resolved to a concrete model id at invocation time by
        :func:`bot.model_preferences.resolve_model_preference`.

        For concurrent-safe usage from async code, prefer :meth:`async_add_agent`.
        """
        if name in self.agents:
            agent = self.agents[name]
            if work_dir:
                agent.work_dir = work_dir
            agent.thread_id = thread_id or agent.thread_id
            agent.description = description or agent.description
            if model:
                agent.model = model
        else:
            agent = Agent(
                name=name,
                work_dir=work_dir,
                description=description,
                agent_type=agent_type,
                model=model,
                thread_id=thread_id,
            )
            self.agents[name] = agent
        self._rebuild_topic_map()
        self.save_state()
        return agent

    async def async_remove_agent(self, name: str) -> bool:
        """Concurrent-safe variant of :meth:`remove_agent`."""
        async with self._agents_lock:
            return self.remove_agent(name)

    def remove_agent(self, name: str) -> bool:
        if name in self.agents and name != "robyx":
            if self.focused_agent == name:
                self.focused_agent = None
            del self.agents[name]
            self._rebuild_topic_map()
            self.save_state()
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
            self.focused_agent = name
            self.save_state()
            return True
        return False

    def clear_focus(self):
        self.focused_agent = None
        self.save_state()

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
