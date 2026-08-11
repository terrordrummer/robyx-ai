"""Canonical ownership identity for scheduled and continuous tasks.

Task delivery targets (for example a dedicated continuous-task topic) are
mutable and therefore cannot be used as an authorization boundary.  The
immutable parent scope is the tuple ``(platform, chat_id, parent_thread_id)``.
It is constructed explicitly at the incoming-message boundary and persisted
unchanged in both queue entries and continuous state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from messaging.base import ChatRef


WORKSPACE_SCOPE_FIELD = "workspace_scope"


def _identifier(raw: Any, *, optional: bool = False) -> str | None:
    if optional and raw in (None, "", "-"):
        return None
    if raw is None:
        raise ValueError("task scope identifier is required")
    value = str(raw).strip()
    if not value or (optional and value == "-"):
        if optional:
            return None
        raise ValueError("task scope identifier is required")
    return value


@dataclass(frozen=True)
class TaskScope:
    """Immutable, JSON-serialisable task ownership scope."""

    platform: str
    chat_id: str
    parent_thread_id: str | None

    def __post_init__(self) -> None:
        platform = str(self.platform or "").strip().lower()
        if platform not in {"telegram", "discord", "slack"}:
            raise ValueError("unsupported task scope platform: %r" % self.platform)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "chat_id", _identifier(self.chat_id))
        object.__setattr__(
            self,
            "parent_thread_id",
            _identifier(self.parent_thread_id, optional=True),
        )

    @classmethod
    def from_chat_ref(
        cls,
        chat_ref: ChatRef,
        parent_thread_id: Any,
    ) -> "TaskScope":
        """Build a scope from an explicit boundary ``ChatRef``.

        This intentionally does not infer a platform from the shape of a chat
        id.  Adapter-specific conversion belongs at the message boundary.
        """
        return cls(
            platform=chat_ref.platform,
            chat_id=chat_ref.chat_id,
            parent_thread_id=parent_thread_id,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskScope":
        if not isinstance(raw, Mapping):
            raise ValueError("task scope must be an object")
        chat_id = raw.get("chat_id")
        if chat_id is None:
            raise ValueError("task scope identifier is required")
        return cls(
            platform=raw.get("platform", ""),
            chat_id=str(chat_id),
            parent_thread_id=raw.get("parent_thread_id"),
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "parent_thread_id": self.parent_thread_id,
        }

    def for_parent_channel(self, channel_id: Any) -> "TaskScope":
        """Return the scope for a newly-created/selected workspace channel.

        Telegram topics remain inside the same chat. Discord and canonical
        Slack refs encode the channel in ``chat_id`` and must replace that
        component as well as ``parent_thread_id``.
        """
        channel = _identifier(channel_id)
        if self.platform == "telegram":
            chat_id = self.chat_id
        else:
            namespace, separator, _old_channel = self.chat_id.partition(":")
            if not separator:
                # A raw Slack channel id is explicit enough only when the
                # operation stays in that same channel; it cannot identify a
                # different channel across workspaces without a team id.
                if self.platform == "slack" and self.chat_id == channel:
                    chat_id = self.chat_id
                else:
                    raise ValueError(
                        "cannot retarget non-canonical %s chat scope"
                        % self.platform
                    )
            else:
                chat_id = "%s:%s" % (namespace, channel)
        return TaskScope(self.platform, chat_id, channel)


def scope_from_record(record: Mapping[str, Any] | None) -> TaskScope | None:
    """Return a validated persisted scope, or ``None`` for a legacy record.

    A present-but-invalid scope is not legacy and must fail closed, so
    ``ValueError`` is allowed to propagate to the caller.
    """
    if not record or WORKSPACE_SCOPE_FIELD not in record:
        return None
    return TaskScope.from_dict(record[WORKSPACE_SCOPE_FIELD])


def attach_scope(record: dict[str, Any], scope: TaskScope) -> dict[str, Any]:
    """Attach the canonical JSON representation to *record* in place."""
    record[WORKSPACE_SCOPE_FIELD] = scope.to_dict()
    return record


def legacy_scope_matches(
    record: Mapping[str, Any],
    current: TaskScope,
    *,
    legacy_parent_thread_id: Any,
    manager: Any = None,
) -> bool:
    """Return whether a legacy record can be migrated without guessing.

    ``parent_thread_id=None`` is inherently shared by unrelated chats and is
    therefore never accepted.  An old record carrying both ``platform`` and
    ``chat_id`` is exact evidence.  Older continuous records normally carry
    neither; those are accepted only for the configured single-host chat and
    when the live ``AgentManager`` proves exactly one parent workspace owns
    the non-null thread.  Slack has no persisted team identity in old data, so
    it deliberately has no host fallback.
    """
    legacy_thread = _identifier(legacy_parent_thread_id, optional=True)
    if legacy_thread is None or current.parent_thread_id != legacy_thread:
        return False

    raw_platform = record.get("platform")
    raw_chat_id = record.get("chat_id")
    if raw_platform not in (None, "") and raw_chat_id not in (None, ""):
        try:
            exact = TaskScope(
                platform=str(raw_platform),
                chat_id=str(raw_chat_id),
                parent_thread_id=legacy_thread,
            )
        except ValueError:
            return False
        return exact == current

    if manager is None:
        return False
    try:
        agents = list(manager.list_active())
    except Exception:
        return False
    owners = [
        agent for agent in agents
        if getattr(agent, "name", None) != "robyx"
        and _identifier(getattr(agent, "thread_id", None), optional=True)
        == legacy_thread
    ]
    if len(owners) != 1:
        return False

    # Config is evidence for the one host this process owns, not a platform
    # inference from the chat-id shape. The current scope was already built
    # explicitly by the adapter boundary.
    try:
        import config
    except ImportError:
        return False
    if str(getattr(config, "PLATFORM", "")).lower() != current.platform:
        return False
    if current.platform == "telegram":
        configured = getattr(config, "CHAT_ID", None)
        return configured is not None and current.chat_id == str(configured)
    if current.platform == "discord":
        configured_guild = getattr(config, "DISCORD_GUILD_ID", None)
        if not configured_guild:
            return False
        guild, separator, _channel = current.chat_id.partition(":")
        return bool(separator) and guild == str(configured_guild)
    return False


__all__ = [
    "WORKSPACE_SCOPE_FIELD",
    "TaskScope",
    "attach_scope",
    "legacy_scope_matches",
    "scope_from_record",
]
