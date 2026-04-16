"""Robyx -- Collaborative workspace data model and store."""

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


class InteractionMode(enum.Enum):
    INTELLIGENT = "intelligent"
    PASSIVE = "passive"


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
            data = json.loads(self._path.read_text())
            for ws_id, ws_data in data.items():
                ws = CollabWorkspace.from_dict(ws_data)
                self._workspaces[ws.id] = ws
            self._rebuild_chat_map()
            log.info("Loaded %d collaborative workspaces", len(self._workspaces))
        except Exception as e:
            log.error(
                "Failed to load collaborative workspaces from %s: %s — "
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
