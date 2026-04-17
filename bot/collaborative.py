"""Robyx -- Collaborative workspace data model and store.

Logging prefix convention (grep-friendly). Emitted from the handler
layer in ``bot/handlers.py``; documented here because the state-machine
lives here:

    collab.announce            -- orchestrator created a pending workspace
    collab.match               -- bot-added matched a pending workspace (Flow A)
    collab.setup.bootstrap     -- ad-hoc bot-added started AI setup (Flow B)
    collab.setup.complete      -- agent emitted [COLLAB_SETUP_COMPLETE]
    collab.send                -- orchestrator emitted [COLLAB_SEND]
    collab.notify_hq           -- group agent emitted [NOTIFY_HQ]
    collab.archive             -- bot removed; workspace closed
    collab.migrate             -- supergroup migration; chat_id rebound
    collab.unauthorised        -- non-authorised user tried to provision
    collab.unsupported_platform-- Discord/Slack add event (not yet supported)
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # POSIX inter-process lock; absent on Windows.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # Windows inter-process lock fallback.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

from config import DATA_DIR

log = logging.getLogger("robyx.collaborative")

COLLAB_FILE = DATA_DIR / "collaborative_workspaces.json"


class Role(enum.Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    PARTICIPANT = "participant"


@dataclass
class CollabWorkspace:
    """A collaborative workspace backed by an external Telegram group."""

    id: str
    name: str
    display_name: str
    agent_name: str
    chat_id: int
    interaction_mode: str = "intelligent"
    parent_workspace: str | None = None
    inherit_memory: bool = True
    invite_link: str | None = None
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    created_by: int = 0
    expected_creator_id: int | None = None
    roles: dict[str, str] = field(default_factory=dict)

    def get_role(self, user_id: int) -> Role | None:
        key = str(user_id)
        role_str = self.roles.get(key)
        if role_str is None:
            return None
        try:
            return Role(role_str)
        except ValueError:
            return None

    def set_role(self, user_id: int, role: Role) -> None:
        self.roles[str(user_id)] = role.value

    def remove_user(self, user_id: int) -> bool:
        return self.roles.pop(str(user_id), None) is not None

    def is_owner(self, user_id: int) -> bool:
        return self.get_role(user_id) == Role.OWNER

    def can_execute(self, user_id: int) -> bool:
        role = self.get_role(user_id)
        return role in (Role.OWNER, Role.OPERATOR)

    def list_users(self) -> list[tuple[int, Role]]:
        result = []
        for uid_str, role_str in self.roles.items():
            try:
                result.append((int(uid_str), Role(role_str)))
            except (ValueError, KeyError):
                continue
        return result

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "agent_name": self.agent_name,
            "chat_id": self.chat_id,
            "interaction_mode": self.interaction_mode,
            "parent_workspace": self.parent_workspace,
            "inherit_memory": self.inherit_memory,
            "invite_link": self.invite_link,
            "status": self.status,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "expected_creator_id": self.expected_creator_id,
            "roles": dict(self.roles),
        }

    @classmethod
    def from_dict(cls, d: dict) -> CollabWorkspace:
        return cls(
            id=d["id"],
            name=d["name"],
            display_name=d.get("display_name", d["name"]),
            agent_name=d["agent_name"],
            chat_id=d["chat_id"],
            interaction_mode=d.get("interaction_mode", "intelligent"),
            parent_workspace=d.get("parent_workspace"),
            inherit_memory=d.get("inherit_memory", True),
            invite_link=d.get("invite_link"),
            status=d.get("status", "active"),
            created_at=d.get("created_at", 0),
            created_by=d.get("created_by", 0),
            expected_creator_id=d.get("expected_creator_id"),
            roles=d.get("roles", {}),
        )


_ROUTABLE_STATUSES = ("active", "setup")


class CollabStore:
    """Persistence layer for collaborative workspaces."""

    def __init__(self, path: Path | None = None):
        self._path = path or COLLAB_FILE
        self._workspaces: dict[str, CollabWorkspace] = {}
        self._chat_map: dict[int, str] = {}
        self._lock = threading.Lock()
        self._load()

    @contextlib.contextmanager
    def _mutex(self):
        """Intra-process + inter-process exclusive access to the store file."""
        with self._lock:
            if fcntl is None and msvcrt is None:
                yield
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._path.with_name(self._path.name + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:  # Windows
                    # Lock a single byte at offset 0. LK_LOCK blocks until free.
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                yield
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        try:
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                finally:
                    os.close(fd)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            # Non-UTF-8 bytes are treated the same as a malformed file:
            # quarantine and start empty. Otherwise the next write would
            # silently overwrite the original bytes.
            from agents import _quarantine_corrupt_file
            _quarantine_corrupt_file(self._path, reason="Decode error: %s" % e)
            log.error(
                "Failed to read collaborative workspaces from %s: %s — "
                "file quarantined, starting with empty registry",
                self._path, e,
            )
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Without quarantine, the next write overwrites the corrupt
            # file and loses every workspace registration silently.
            # Closes Pass 1 F17 (deferred).
            from agents import _quarantine_corrupt_file
            _quarantine_corrupt_file(self._path, reason="JSONDecodeError: %s" % e)
            log.error(
                "Collaborative workspaces file %s is corrupt — quarantined. "
                "Re-add the bot to each collaborative group to rebuild state.",
                self._path,
            )
            return
        try:
            for ws_id, ws_data in data.items():
                ws = CollabWorkspace.from_dict(ws_data)
                self._workspaces[ws.id] = ws
            self._rebuild_chat_map()
            log.info("Loaded %d collaborative workspaces", len(self._workspaces))
        except Exception as e:
            log.error(
                "Failed to parse collaborative workspaces from %s: %s — "
                "collaborative routing is DEGRADED until this is fixed",
                self._path, e,
            )

    def _rebuild_chat_map(self) -> None:
        self._chat_map = {}
        for ws in self._workspaces.values():
            if ws.status in _ROUTABLE_STATUSES and ws.chat_id:
                self._chat_map[ws.chat_id] = ws.id

    def _write_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {ws_id: ws.to_dict() for ws_id, ws in self._workspaces.items()}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    def _save(self) -> None:
        with self._mutex():
            self._write_unlocked()

    def add(self, ws: CollabWorkspace) -> None:
        with self._mutex():
            self._workspaces[ws.id] = ws
            self._rebuild_chat_map()
            self._write_unlocked()

    def remove(self, ws_id: str) -> bool:
        with self._mutex():
            if ws_id not in self._workspaces:
                return False
            del self._workspaces[ws_id]
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def close(self, ws_id: str) -> bool:
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            ws.status = "closed"
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def purge_closed(self) -> int:
        """Drop every workspace in ``status="closed"`` from the store.

        Closed workspaces linger in-memory/on-disk so that operators can
        audit history; call this from a maintenance command when the
        backlog gets too large. Returns the number of entries removed.
        """
        with self._mutex():
            closed_ids = [
                ws_id for ws_id, ws in self._workspaces.items()
                if ws.status == "closed"
            ]
            for ws_id in closed_ids:
                del self._workspaces[ws_id]
            if closed_ids:
                self._rebuild_chat_map()
                self._write_unlocked()
            return len(closed_ids)

    def get(self, ws_id: str) -> CollabWorkspace | None:
        return self._workspaces.get(ws_id)

    def get_by_chat_id(self, chat_id: int) -> CollabWorkspace | None:
        ws_id = self._chat_map.get(chat_id)
        return self._workspaces.get(ws_id) if ws_id else None

    def get_by_agent_name(self, agent_name: str) -> CollabWorkspace | None:
        for ws in self._workspaces.values():
            if ws.agent_name == agent_name and ws.status == "active":
                return ws
        return None

    def list_active(self) -> list[CollabWorkspace]:
        return [ws for ws in self._workspaces.values() if ws.status == "active"]

    def list_all(self) -> list[CollabWorkspace]:
        """Return every workspace, regardless of status."""
        return list(self._workspaces.values())

    def list_pending_for_agent(self, agent_name: str) -> list[CollabWorkspace]:
        return [
            ws for ws in self._workspaces.values()
            if ws.agent_name == agent_name
            and ws.status == "pending"
        ]

    def list_pending_for_creator(self, creator_id: int) -> list[CollabWorkspace]:
        """Return pending workspaces explicitly bound to this creator id."""
        return [
            ws for ws in self._workspaces.values()
            if ws.status == "pending"
            and ws.chat_id == 0
            and ws.expected_creator_id == creator_id
        ]

    def create_pending(
        self,
        *,
        name: str,
        display_name: str,
        agent_name: str,
        parent_workspace: str | None,
        inherit_memory: bool,
        creator_id: int,
    ) -> CollabWorkspace:
        """Persist a pre-announced collaborative workspace.

        Used by the orchestrator's ``[COLLAB_ANNOUNCE ...]`` handler
        before the external Telegram group exists. The resulting record
        has ``status="pending"`` and ``chat_id=0``; it is bound to a
        real chat_id by ``update_chat_id`` when the bot is later added
        to the matching group.

        Raises ``ValueError`` on: blank ``name``; ``creator_id == 0``;
        collision with any existing workspace ``name``. The caller is
        responsible for writing the seed ``data/agents/<name>.md`` file
        *before* calling this method (matches the ordering convention
        that closes the "agent registered but file missing" race; see
        ``bot/handlers.py:1468-1506``).
        """
        if not name:
            raise ValueError("name must not be empty")
        if creator_id == 0:
            raise ValueError("creator_id must not be zero")
        with self._mutex():
            for existing in self._workspaces.values():
                if existing.name == name:
                    raise ValueError("name collision: %s" % name)
            ws_id = "collab-%s" % name
            # Uniqueness on id: if someone pre-announced "nebula" before,
            # then the old one was closed and the name freed, the id would
            # collide. Append a short suffix to keep ids unique.
            if ws_id in self._workspaces:
                import uuid as _uuid
                ws_id = "%s-%s" % (ws_id, _uuid.uuid4().hex[:6])
            ws = CollabWorkspace(
                id=ws_id,
                name=name,
                display_name=display_name,
                agent_name=agent_name,
                chat_id=0,
                interaction_mode="intelligent",
                parent_workspace=parent_workspace,
                inherit_memory=inherit_memory,
                status="pending",
                created_by=creator_id,
                expected_creator_id=creator_id,
                roles={str(creator_id): Role.OWNER.value},
            )
            self._workspaces[ws.id] = ws
            self._rebuild_chat_map()
            self._write_unlocked()
            return ws

    def finalize_setup(
        self,
        ws_id: str,
        *,
        parent_workspace: str | None,
        inherit_memory: bool,
    ) -> bool:
        """Promote a ``setup`` workspace to ``active`` after the AI-driven
        setup conversation emitted ``[COLLAB_SETUP_COMPLETE ...]``.

        Refuses to act unless the workspace is currently ``setup``; other
        statuses return ``False`` and log a warning. The caller is
        responsible for rewriting ``data/agents/<name>.md`` *before*
        calling this (ordering matches ``create_pending``).
        """
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            if ws.status != "setup":
                log.warning(
                    "Refusing finalize_setup for %s: status=%s (expected 'setup')",
                    ws_id, ws.status,
                )
                return False
            ws.parent_workspace = parent_workspace
            ws.inherit_memory = inherit_memory
            ws.status = "active"
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> bool:
        """Rebind a workspace from ``old_chat_id`` to ``new_chat_id``
        without changing status. Used for Telegram supergroup migration.

        Refuses to act unless there is a workspace bound to
        ``old_chat_id`` in a routable status (active or setup).
        """
        if new_chat_id == 0:
            return False
        with self._mutex():
            ws_id = self._chat_map.get(old_chat_id)
            ws = self._workspaces.get(ws_id) if ws_id else None
            if not ws:
                log.warning(
                    "Refusing migrate_chat_id: no routable workspace at chat_id=%s",
                    old_chat_id,
                )
                return False
            ws.chat_id = new_chat_id
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def list_for_orchestrator(self) -> list[dict]:
        """Return the live-group registry for injection into the
        orchestrator's system prompt.

        Excludes closed workspaces; includes active, setup, and pending
        (chat_id may be 0 for pending). Sorted by ``created_at`` desc.
        ``purpose`` is a best-effort read of the first non-heading,
        non-blank line from ``data/agents/<name>.md``; falls back to
        ``display_name`` when the file is absent or unreadable.
        """
        from config import AGENTS_DIR
        out: list[dict] = []
        for ws in self._workspaces.values():
            if ws.status == "closed":
                continue
            purpose = ws.display_name
            agent_file = AGENTS_DIR / ("%s.md" % ws.agent_name)
            try:
                if agent_file.exists():
                    for line in agent_file.read_text().splitlines():
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#"):
                            continue
                        purpose = stripped
                        break
            except OSError:
                pass
            out.append({
                "name": ws.name,
                "display_name": ws.display_name,
                "purpose": purpose,
                "chat_id": ws.chat_id,
                "status": ws.status,
            })
        out.sort(key=lambda d: next(
            (w.created_at for w in self._workspaces.values() if w.name == d["name"]),
            0,
        ), reverse=True)
        return out

    def update_chat_id(
        self,
        ws_id: str,
        chat_id: int,
        *,
        expected_creator_id: int | None = None,
    ) -> bool:
        """Bind a pending workspace to a chat_id and promote it to active.

        Refuses to promote a workspace unless it is currently ``pending`` and
        still unlinked (``chat_id == 0``). When ``expected_creator_id`` is
        provided, it must match the workspace's bound creator — this prevents
        a Flow-A race where an outsider adds the bot to a group and hijacks
        another user's pending workspace.
        """
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            if ws.status != "pending" or ws.chat_id != 0:
                log.warning(
                    "Refusing update_chat_id for %s: status=%s chat_id=%s",
                    ws_id, ws.status, ws.chat_id,
                )
                return False
            if (
                expected_creator_id is not None
                and ws.expected_creator_id is not None
                and ws.expected_creator_id != expected_creator_id
            ):
                log.warning(
                    "Refusing update_chat_id for %s: creator mismatch "
                    "(expected=%s got=%s)",
                    ws_id, ws.expected_creator_id, expected_creator_id,
                )
                return False
            ws.chat_id = chat_id
            ws.status = "active"
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def update_roles(self, ws_id: str, user_id: int, role: Role) -> bool:
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            ws.set_role(user_id, role)
            self._write_unlocked()
            return True

    def update_interaction_mode(self, ws_id: str, mode: str) -> bool:
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            if mode not in ("intelligent", "passive"):
                return False
            ws.interaction_mode = mode
            self._write_unlocked()
            return True

    def update_invite_link(self, ws_id: str, link: str) -> bool:
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            ws.invite_link = link
            self._write_unlocked()
            return True

    @property
    def chat_ids(self) -> set[int]:
        return set(self._chat_map.keys())
