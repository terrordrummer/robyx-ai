"""Robyx -- Collaborative workspace data model and store."""

from __future__ import annotations

import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
            roles=d.get("roles", {}),
        )


class CollabStore:
    """Persistence layer for collaborative workspaces."""

    def __init__(self, path: Path | None = None):
        self._path = path or COLLAB_FILE
        self._workspaces: dict[str, CollabWorkspace] = {}
        self._chat_map: dict[int, str] = {}
        self._load()

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
            log.warning("Failed to load collaborative workspaces: %s", e)

    def _rebuild_chat_map(self) -> None:
        self._chat_map = {}
        for ws in self._workspaces.values():
            if ws.status == "active" and ws.chat_id:
                self._chat_map[ws.chat_id] = ws.id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {ws_id: ws.to_dict() for ws_id, ws in self._workspaces.items()}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    def add(self, ws: CollabWorkspace) -> None:
        self._workspaces[ws.id] = ws
        self._rebuild_chat_map()
        self._save()

    def remove(self, ws_id: str) -> bool:
        if ws_id not in self._workspaces:
            return False
        del self._workspaces[ws_id]
        self._rebuild_chat_map()
        self._save()
        return True

    def close(self, ws_id: str) -> bool:
        ws = self._workspaces.get(ws_id)
        if not ws:
            return False
        ws.status = "closed"
        self._rebuild_chat_map()
        self._save()
        return True

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

    def list_pending_for_agent(self, agent_name: str) -> list[CollabWorkspace]:
        return [
            ws for ws in self._workspaces.values()
            if ws.agent_name == agent_name
            and ws.status == "pending"
        ]

    def update_chat_id(self, ws_id: str, chat_id: int) -> bool:
        ws = self._workspaces.get(ws_id)
        if not ws:
            return False
        ws.chat_id = chat_id
        ws.status = "active"
        self._rebuild_chat_map()
        self._save()
        return True

    def update_roles(self, ws_id: str, user_id: int, role: Role) -> bool:
        ws = self._workspaces.get(ws_id)
        if not ws:
            return False
        ws.set_role(user_id, role)
        self._save()
        return True

    def update_interaction_mode(self, ws_id: str, mode: str) -> bool:
        ws = self._workspaces.get(ws_id)
        if not ws:
            return False
        if mode not in ("intelligent", "passive"):
            return False
        ws.interaction_mode = mode
        self._save()
        return True

    def update_invite_link(self, ws_id: str, link: str) -> bool:
        ws = self._workspaces.get(ws_id)
        if not ws:
            return False
        ws.invite_link = link
        self._save()
        return True

    @property
    def chat_ids(self) -> set[int]:
        return set(self._chat_map.keys())
