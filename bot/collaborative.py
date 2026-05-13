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
import re
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
from messaging.base import ChatRef

log = logging.getLogger("robyx.collaborative")

COLLAB_FILE = DATA_DIR / "collaborative_workspaces.json"


# ── Spec 007 — chat-id encoding helpers (Discord) ────────────────────────


def parse_discord_chat_id(chat_id: str) -> tuple[int, int]:
    """Split a canonical Discord ``chat_id`` (``"<guild>:<channel>"``)
    into the integer guild and channel ids.

    Raises ``ValueError`` on malformed input.
    """
    parts = chat_id.split(":", 1)
    if len(parts) != 2:
        raise ValueError("malformed Discord chat_id: %r" % chat_id)
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as e:
        raise ValueError(
            "non-integer Discord chat_id components: %r (%s)" % (chat_id, e),
        ) from e


def make_discord_chat_id(guild_id: int, channel_id: int) -> str:
    """Build the canonical Discord ``chat_id`` string."""
    return "%d:%d" % (guild_id, channel_id)


def _normalise_chat_ref(value: Any, *, default_platform: str = "telegram") -> ChatRef:
    """Coerce a legacy raw chat_id or a ChatRef instance into a ChatRef.

    Used by :class:`CollabStore` to keep backwards compatibility with
    pre-007 callers that pass a raw integer ``chat_id`` (assumed
    Telegram). New code SHOULD pass a :class:`ChatRef` explicitly so the
    platform is unambiguous.
    """
    if isinstance(value, ChatRef):
        return value
    return ChatRef(platform=default_platform, chat_id=str(value))

# Shape that a collaborative workspace name is allowed to take. Names are
# used as filename segments (``data/agents/<name>.md``) and as the suffix
# of the workspace id (``collab-<name>``); Flow B ad-hoc setup already
# sanitises via ``re.sub(r'[^a-z0-9-]', '-', …)`` at ``handlers.py``, but
# Flow A (``[COLLAB_ANNOUNCE name="…"]``) previously received AI-emitted
# names verbatim, so a name like ``../../evil`` would have written AI
# content outside ``AGENTS_DIR``. Pass 2 P2-81.
_VALID_COLLAB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_collab_name(name: str) -> str:
    """Return ``name`` if it is a safe workspace name, or raise ``ValueError``.

    Enforces the invariant that collaborative workspace names are
    lowercase alphanumeric plus hyphens, 1–64 characters, starting with
    an alphanumeric character. This is the same alphabet Flow B produces
    via ``_sanitize_task_name`` / ``re.sub(r'[^a-z0-9-]', '-', …)`` — so
    tightening Flow A to pass through this validator does not reject any
    legitimate Flow-B name.

    Rejects empty / whitespace-only strings, path separators (``/``,
    ``\\``), control characters (newline, tab, null), `..`, `.`, mixed
    case, underscores, dots, spaces, and anything longer than 64 chars.

    Called from:
    * ``handlers._handle_collab_announce`` — before the AI-controlled
      attrs reach ``AGENTS_DIR / (name + ".md")`` for ``write_text``.
    * ``CollabStore.create_pending`` — defense-in-depth (in case a
      future caller bypasses the handler).
    """
    value = str(name or "").strip()
    if not _VALID_COLLAB_NAME_RE.match(value):
        raise ValueError(
            "invalid collaborative workspace name %r: must match "
            "[a-z0-9][a-z0-9-]{0,63} (lowercase alphanumeric + hyphens, "
            "1-64 chars, starting alphanumeric)" % name
        )
    return value


class Role(enum.Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    PARTICIPANT = "participant"


@dataclass
class CollabWorkspace:
    """A collaborative workspace backed by an external chat on Telegram,
    Discord (post-spec-007), or Slack (post-spec-008).

    ``chat_id`` is the canonical string form per
    :class:`bot.messaging.base.ChatRef`. Legacy on-disk records that
    stored ``chat_id`` as ``int`` are accepted by :meth:`from_dict` and
    coerced to ``str``; the on-disk migration ``v0_28_0`` normalises the
    persisted file. ``__post_init__`` coerces in-memory construction so
    existing tests that pass ``chat_id=-100…`` (int) keep working.
    """

    id: str
    name: str
    display_name: str
    agent_name: str
    chat_id: str = "0"
    interaction_mode: str = "intelligent"
    parent_workspace: str | None = None
    inherit_memory: bool = True
    invite_link: str | None = None
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    created_by: int | str = 0
    expected_creator_id: int | str | None = None
    roles: dict[str, str] = field(default_factory=dict)
    # Spec 007 additions.
    platform: str = "telegram"
    expected_platform: str | None = None

    def __post_init__(self) -> None:
        # Coerce legacy int chat_ids passed by pre-spec-007 callers
        # (tests, in-process state) to the canonical string form. The
        # migration v0_28_0 normalises the on-disk representation.
        if not isinstance(self.chat_id, str):
            self.chat_id = str(self.chat_id)

    @property
    def chat_ref(self) -> ChatRef:
        """Platform-agnostic identifier for this workspace's chat."""
        return ChatRef(platform=self.platform, chat_id=self.chat_id)

    def get_role(self, user_id: int | str) -> Role | None:
        key = str(user_id)
        role_str = self.roles.get(key)
        if role_str is None:
            return None
        try:
            return Role(role_str)
        except ValueError:
            return None

    def set_role(self, user_id: int | str, role: Role) -> None:
        self.roles[str(user_id)] = role.value

    def remove_user(self, user_id: int | str) -> bool:
        return self.roles.pop(str(user_id), None) is not None

    def is_owner(self, user_id: int | str) -> bool:
        return self.get_role(user_id) == Role.OWNER

    def can_execute(self, user_id: int | str) -> bool:
        role = self.get_role(user_id)
        return role in (Role.OWNER, Role.OPERATOR)

    def list_users(self) -> list[tuple[int | str, Role]]:
        """Return ``(user_id, role)`` pairs. ``user_id`` is returned as
        ``int`` when the stored key parses as integer (Telegram/Discord
        ids), or as ``str`` otherwise (Slack ``"U…"`` ids — reserved for
        spec 008)."""
        result: list[tuple[int | str, Role]] = []
        for uid_str, role_str in self.roles.items():
            try:
                role = Role(role_str)
            except (ValueError, KeyError):
                continue
            try:
                uid: int | str = int(uid_str)
            except ValueError:
                uid = uid_str
            result.append((uid, role))
        return result

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "agent_name": self.agent_name,
            "chat_id": str(self.chat_id),
            "platform": self.platform,
            "expected_platform": self.expected_platform,
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
        raw_chat_id = d.get("chat_id", "0")
        chat_id = str(raw_chat_id) if raw_chat_id is not None else "0"
        return cls(
            id=d["id"],
            name=d["name"],
            display_name=d.get("display_name", d["name"]),
            agent_name=d["agent_name"],
            chat_id=chat_id,
            platform=d.get("platform", "telegram"),
            expected_platform=d.get("expected_platform"),
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
        # Spec 007: keyed by (platform, chat_id) tuple. Pre-007 stored
        # ``dict[int, str]`` (Telegram-only). Pending workspaces with
        # ``chat_id == "0"`` are NOT in the map.
        self._chat_map: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._load()

    @contextlib.contextmanager
    def _mutex(self):
        """Intra-process + inter-process exclusive access to the store file.

        Invariants this lock upholds:

        * Writes are serialized across threads in the same process (the
          thread lock at ``self._lock``).
        * Writes are serialized across processes by an fcntl/msvcrt file
          lock on ``<path>.lock``, so two processes cannot interleave
          their writes even if both hold stale in-memory state.
        * The lock does NOT cover :meth:`_load`. ``_load`` is only called
          from ``__init__``, and :func:`bot.bot.ensure_single_instance`
          keeps a PID/file lock that prevents two bot processes from
          running against the same ``data/`` directory concurrently — so
          the "load + later write" read-modify-write cycle is safe for
          our deployment model. Do NOT add out-of-band callers to
          ``_load`` without first wrapping them in ``_mutex``.
        """
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

        from agents import _quarantine_corrupt_file, _recover_from_snapshot

        # Two attempts: the original file, then whatever recovery was
        # able to install (if anything). See AgentManager._load_state for
        # the same pattern.
        for attempt in range(2):
            try:
                raw = self._path.read_text()
            except (OSError, UnicodeDecodeError) as e:
                if attempt == 0:
                    _quarantine_corrupt_file(self._path, reason="Decode error: %s" % e)
                    if _recover_from_snapshot(self._path, reason="Decode error: %s" % e):
                        continue
                log.error(
                    "Failed to read collaborative workspaces from %s: %s — "
                    "file quarantined, starting with empty registry",
                    self._path, e,
                )
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                if attempt == 0:
                    _quarantine_corrupt_file(self._path, reason="JSONDecodeError: %s" % e)
                    if _recover_from_snapshot(self._path, reason="JSONDecodeError: %s" % e):
                        continue
                log.error(
                    "Collaborative workspaces file %s is corrupt — quarantined. "
                    "Re-add the bot to each collaborative group to rebuild state.",
                    self._path,
                )
                return
            break  # parse succeeded
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
            if ws.status in _ROUTABLE_STATUSES and ws.chat_id and ws.chat_id != "0":
                self._chat_map[(ws.platform, ws.chat_id)] = ws.id

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

    def get_by_chat_id(self, chat_ref_or_id: Any) -> CollabWorkspace | None:
        """Look up an active/setup workspace by its chat identifier.

        Spec 007: accepts either a :class:`ChatRef` (preferred) or a
        legacy raw chat_id (``int`` / ``str`` — assumed Telegram) for
        backwards compatibility with pre-007 callers. New code SHOULD
        pass ``ChatRef`` so the platform is explicit.
        """
        chat_ref = _normalise_chat_ref(chat_ref_or_id)
        ws_id = self._chat_map.get((chat_ref.platform, chat_ref.chat_id))
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

    def list_pending_for_creator(
        self,
        creator_id: int | str,
        *,
        platform: str | None = None,
    ) -> list[CollabWorkspace]:
        """Return pending workspaces explicitly bound to this creator id.

        Spec 007: optional ``platform`` filter rejects pending records
        whose ``expected_platform`` does not match (cross-platform user
        id collision guard, FR-013). When ``platform`` is ``None`` (the
        default — preserves pre-007 Telegram callers), records that
        have no ``expected_platform`` set OR have it set to any value
        are returned. When ``platform`` is given, a record is included
        iff ``expected_platform`` is ``None`` or equal to ``platform``.
        """
        out: list[CollabWorkspace] = []
        for ws in self._workspaces.values():
            if ws.status != "pending":
                continue
            if ws.chat_id != "0":
                continue
            if ws.expected_creator_id != creator_id:
                continue
            if (
                platform is not None
                and ws.expected_platform is not None
                and ws.expected_platform != platform
            ):
                continue
            out.append(ws)
        return out

    def create_pending(
        self,
        *,
        name: str,
        display_name: str,
        agent_name: str,
        parent_workspace: str | None,
        inherit_memory: bool,
        creator_id: int | str,
        platform: str = "telegram",
        expected_platform: str | None = None,
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
        # validate_collab_name raises on blank / path-traversal / mixed-case;
        # handlers._handle_collab_announce is expected to reject up-front, but
        # this guard is defense-in-depth in case a future caller bypasses it.
        name = validate_collab_name(name)
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
                chat_id="0",
                platform=platform,
                expected_platform=expected_platform,
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

    def migrate_chat_id(self, old: Any, new: Any) -> bool:
        """Rebind a workspace from ``old`` to ``new`` without changing
        status. Used for Telegram supergroup migration.

        Spec 007: accepts either :class:`ChatRef` instances (preferred)
        or legacy raw chat_ids (assumed Telegram). Refuses cross-platform
        migrations — both ``old`` and ``new`` MUST share the same
        platform.
        """
        old_ref = _normalise_chat_ref(old)
        new_ref = _normalise_chat_ref(new)
        if old_ref.platform != new_ref.platform:
            log.warning(
                "Refusing migrate_chat_id: cross-platform migration %s → %s",
                old_ref.platform, new_ref.platform,
            )
            return False
        if not new_ref.chat_id or new_ref.chat_id == "0":
            return False
        with self._mutex():
            ws_id = self._chat_map.get((old_ref.platform, old_ref.chat_id))
            ws = self._workspaces.get(ws_id) if ws_id else None
            if not ws:
                log.warning(
                    "Refusing migrate_chat_id: no routable workspace at %s/%s",
                    old_ref.platform, old_ref.chat_id,
                )
                return False
            ws.chat_id = new_ref.chat_id
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
        chat_ref_or_id: Any,
        *,
        expected_creator_id: int | str | None = None,
    ) -> bool:
        """Bind a pending workspace to a chat reference and promote to active.

        Spec 007: accepts either a :class:`ChatRef` (preferred) or a
        legacy raw chat_id (``int`` / ``str`` — assumed Telegram).
        Refuses cross-platform binds when the pending workspace has
        ``expected_platform`` set and the new chat reference's platform
        does not match (FR-013). Refuses to promote a workspace unless
        it is currently ``pending`` and still unlinked (``chat_id == "0"``).
        Refuses creator mismatches in the same conditions as pre-007.
        """
        new_ref = _normalise_chat_ref(chat_ref_or_id)
        with self._mutex():
            ws = self._workspaces.get(ws_id)
            if not ws:
                return False
            if ws.status != "pending" or ws.chat_id != "0":
                log.warning(
                    "Refusing update_chat_id for %s: status=%s chat_id=%s",
                    ws_id, ws.status, ws.chat_id,
                )
                return False
            if (
                ws.expected_platform is not None
                and ws.expected_platform != new_ref.platform
            ):
                log.warning(
                    "Refusing update_chat_id for %s: platform mismatch "
                    "(expected=%s got=%s)",
                    ws_id, ws.expected_platform, new_ref.platform,
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
            ws.platform = new_ref.platform
            ws.chat_id = new_ref.chat_id
            ws.status = "active"
            self._rebuild_chat_map()
            self._write_unlocked()
            return True

    def update_roles(self, ws_id: str, user_id: int | str, role: Role) -> bool:
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
        """Legacy Telegram-only routable chat_ids as a ``set[int]``.

        Preserved for backwards compatibility with pre-spec-007 callers
        (currently only one assertion in ``tests/test_collaborative.py``).
        For platform-aware iteration use :meth:`find_active_by_platform`
        or :meth:`chat_keys`.
        """
        out: set[int] = set()
        for plat, chat_id in self._chat_map:
            if plat != "telegram":
                continue
            try:
                out.add(int(chat_id))
            except ValueError:
                continue
        return out

    @property
    def chat_keys(self) -> set[tuple[str, str]]:
        """Multi-platform routable chat keys as ``set[(platform, chat_id)]``.

        Spec 007: introduced as the platform-aware replacement for
        :attr:`chat_ids`. Handler-layer callers migrate to this in
        spec 007 Phase 3.
        """
        return set(self._chat_map.keys())

    def find_active_in_guild(self, guild_id: str | int) -> list[CollabWorkspace]:
        """Return active/setup Discord workspaces whose ``chat_id``
        starts with ``f"{guild_id}:"``.

        Used by the spec-007 ``leave_chat`` policy in
        ``bot/handlers.py:collab_bot_added`` to refuse leaving a guild
        that hosts other legitimate workspaces. ``guild_id`` is accepted
        as ``str`` or ``int`` for caller ergonomics.
        """
        prefix = "%s:" % guild_id
        return [
            ws for ws in self._workspaces.values()
            if ws.platform == "discord"
            and ws.status in _ROUTABLE_STATUSES
            and ws.chat_id.startswith(prefix)
        ]

    def find_active_by_platform(self, platform: str) -> list[CollabWorkspace]:
        """Return active/setup workspaces on a given platform.

        Reserved for spec 008 (Slack) but harmless to expose now.
        """
        return [
            ws for ws in self._workspaces.values()
            if ws.platform == platform and ws.status in _ROUTABLE_STATUSES
        ]
