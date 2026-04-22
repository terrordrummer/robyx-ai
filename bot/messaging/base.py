"""Robyx — Abstract platform interface."""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

_log = logging.getLogger("robyx.messaging")

_T = TypeVar("_T")


async def retry_send(
    op: Callable[[], Awaitable[_T]],
    *,
    label: str = "platform send",
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> _T:
    """Run *op* with exponential backoff on transient exceptions.

    Used by adapters to shield ``send_message`` (and friends) from
    momentary platform hiccups (network blips, 5xx responses) without
    each adapter reimplementing the retry loop. The final failure is
    re-raised so callers can surface it to the user.
    """
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await op()
        except Exception as exc:  # noqa: BLE001 - platforms raise heterogeneous types
            last_exc = exc
            if attempt >= max_attempts:
                break
            _log.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.1fs",
                label, attempt, max_attempts, exc, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
    assert last_exc is not None
    _log.error("%s failed after %d attempts: %s", label, max_attempts, last_exc)
    raise last_exc


class TopicUnreachable(Exception):
    """Raised by adapter topic operations when the target topic/channel has
    been deleted, archived irreversibly, or is otherwise permanently
    inaccessible. Callers (scheduler, delivery) catch this and invoke the
    FR-002a last-resort HQ surface path (spec 006).

    Transient failures (rate limits, network blips) are NOT this — those
    are retried internally by :func:`retry_send` and surface as regular
    falsy returns if retries are exhausted.
    """

    def __init__(self, channel_id: Any, reason: str = "") -> None:
        self.channel_id = channel_id
        self.reason = reason
        super().__init__(
            "Topic %s unreachable: %s" % (channel_id, reason or "unknown"),
        )


@dataclass
class PlatformMessage:
    """Platform-agnostic incoming message."""

    user_id: Any
    chat_id: Any
    text: str | None = None
    thread_id: Any = None
    voice_file_id: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    user_name: str | None = None


class Platform(abc.ABC):
    """Abstract interface that every messaging platform adapter must implement."""

    @property
    @abc.abstractmethod
    def max_message_length(self) -> int:
        """Maximum length of a single message on this platform."""

    @property
    def max_photo_bytes(self) -> int:
        """Maximum photo upload size in bytes. Default 10 MB (Telegram limit).

        Adapters should override if their platform has a tighter ceiling.
        Used by outgoing-image compression to decide whether the source file
        needs to be re-encoded as JPEG before upload.
        """
        return 10 * 1024 * 1024

    @property
    def bot_username(self) -> str | None:
        """Return the bot's at-handle without the leading ``@`` (or None).

        Used by the collaborative-workspace routing to detect explicit
        mentions (``@robyx_bot``) in passive-mode groups. Adapters that
        know their username should override and return it; the default
        returns ``None``, which means "no mention detection".
        """
        return None

    @property
    @abc.abstractmethod
    def control_room_id(self) -> Any:
        """Thread/channel ID for the control room (General topic).

        The concrete type is platform-specific:
        - Telegram / Discord: integer ids
        - Slack: channel-id strings
        - ``None``: no thread routing
        """

    @abc.abstractmethod
    def is_owner(self, user_id: int) -> bool:
        """Return True if *user_id* is the bot owner."""

    @abc.abstractmethod
    def is_main_thread(self, chat_id: Any, thread_id: Any) -> bool:
        """Return True if ``(chat_id, thread_id)`` identifies the platform's
        main destination — i.e. where the orchestrator (Robyx) lives.

        Semantics per platform:
        - Telegram: the General topic of the forum supergroup (``thread_id is None``)
        - Discord:  the control channel (``thread_id == control_channel_id``)
        - Slack:    top-level messages in the control-room channel
        """

    @abc.abstractmethod
    async def reply(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> Any:
        """Reply to a specific message. Returns an opaque reference to the sent message.

        See :meth:`send_message` for the ``parse_mode`` contract.
        """

    @abc.abstractmethod
    async def edit_message(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> None:
        """Edit a previously sent message identified by *msg_ref*.

        See :meth:`send_message` for the ``parse_mode`` contract.
        """

    @abc.abstractmethod
    async def send_message(
        self,
        chat_id: Any,
        text: str,
        thread_id: Any = None,
        parse_mode: str | None = None,
    ) -> Any:
        """Send a new message to a chat/channel. Returns an opaque message reference.

        ``parse_mode`` is normalized to a single recognized value:

        * ``"markdown"`` — request markdown rendering on platforms that
          support an opt-in flag (Telegram). Platforms that always render
          markdown (Slack, Discord) ignore it. Platforms that never do
          (none currently) ignore it. Other string values are reserved
          for Telegram-specific modes (e.g. ``"HTML"``) and treated as
          opaque pass-through on Telegram, ignored elsewhere.
        * ``None`` — let the platform pick its default (plain text on
          Telegram replies, native rendering on Slack/Discord).
        """

    @abc.abstractmethod
    async def send_typing(self, chat_id: Any, thread_id: Any = None) -> None:
        """Send a typing indicator."""

    @abc.abstractmethod
    async def send_photo(
        self,
        chat_id: Any,
        path: str,
        caption: str | None = None,
        thread_id: Any = None,
    ) -> Any:
        """Upload and send an image file.

        The adapter is responsible for:
        - compressing/re-encoding the file if it exceeds ``max_photo_bytes``
          (use ``media.prepare_image_for_upload``)
        - cleaning up any temporary file produced by compression
        - routing to the correct chat/channel/thread

        Returns an opaque reference to the sent message, or ``None`` on
        failure (failures are logged, never raised, so the outer handler
        can append a textual error to the user reply).
        """

    @abc.abstractmethod
    async def download_voice(self, file_id: str) -> str:
        """Download a voice file and return the local temp path."""

    @abc.abstractmethod
    async def create_channel(self, name: str) -> Any:
        """Create a new channel/topic. Returns the channel id or None on failure."""

    @abc.abstractmethod
    async def close_channel(self, channel_id: Any) -> bool:
        """Close/archive a channel/topic."""

    @abc.abstractmethod
    async def send_to_channel(self, channel_id: Any, text: str, parse_mode: str | None = None) -> bool:
        """Send a message to a specific channel/topic.

        See :meth:`send_message` for the ``parse_mode`` contract. Note that
        on Telegram this method targets forum topics, where agent output
        is markdown-formatted by convention: ``parse_mode=None`` defaults
        to ``"markdown"`` rendering. Pass ``parse_mode=""`` (empty string)
        to force plain text on Telegram topics.
        """

    async def get_invite_link(self, chat_id: Any) -> str | None:
        """Generate or retrieve an invite link for a chat/group.

        Default: not supported (returns None). Adapters should override
        for platforms that support invite links.
        """
        return None

    async def leave_chat(self, chat_id: Any) -> None:
        """Leave a chat/group. Used by the unauthorised-adder guard for
        external collaborative groups.

        Default: not supported — adapters that cannot implement this
        MUST override and raise ``NotImplementedError``. Telegram
        implements it via the bot API; Discord/Slack raise for now
        (external groups are Telegram-only in this iteration).
        """
        raise NotImplementedError(
            "leave_chat is not supported on this platform",
        )

    @abc.abstractmethod
    async def rename_main_channel(self, display_name: str, slug: str) -> bool:
        """Rename the platform's main destination (where Robyx lives).

        Arguments:
            display_name: human-readable name (e.g. ``"Headquarters"``).
                Platforms that support spaces and mixed case (Telegram) use
                this verbatim.
            slug: slugified form (e.g. ``"headquarters"``). Platforms with
                naming restrictions (Discord/Slack — lowercase, no spaces)
                use this.

        Returns:
            ``True`` on success, or if the channel was already renamed and
            the operation is a no-op. ``False`` on failure (logged by the
            adapter). Must NOT raise — the migration runner depends on a
            clean boolean.
        """

    # ── Spec 006 — dedicated-topic operations ───────────────────────────

    async def edit_topic_title(self, channel_id: Any, new_title: str) -> bool:
        """Update the display title of a topic/channel.

        Default implementation: log WARN once and return ``False``.
        Adapters MUST override with a real implementation. Raises
        :class:`TopicUnreachable` if the topic has been permanently
        deleted (e.g. user removed it manually).

        Returns True on success, False on transient / non-fatal failure.
        """
        _log.warning(
            "edit_topic_title: platform %s does not implement topic title edits",
            type(self).__name__,
        )
        return False

    async def pin_message(
        self,
        chat_id: Any,
        thread_id: Any,
        message_id: Any,
    ) -> bool:
        """Pin a specific message inside a topic.

        Default: WARN + ``False``. Telegram/Discord implement this;
        Slack pins are workspace-wide (documented degradation — adapter
        logs one WARN per session per FR-013).
        """
        _log.warning(
            "pin_message: platform %s does not implement per-topic pinning",
            type(self).__name__,
        )
        return False

    async def unpin_message(
        self,
        chat_id: Any,
        thread_id: Any,
        message_id: Any | None = None,
    ) -> bool:
        """Unpin a specific message (or all pinned messages in the topic
        if ``message_id`` is None).

        Default: WARN + ``False``. Adapters MUST override.
        """
        _log.warning(
            "unpin_message: platform %s does not implement unpin",
            type(self).__name__,
        )
        return False

    async def close_topic(self, channel_id: Any) -> bool:
        """Close a topic/channel to new messages. History remains visible.

        On Telegram maps to ``closeForumTopic``. On Discord to
        ``thread.edit(archived=True, locked=True)``. On Slack to
        ``conversations.archive`` (permanent — logged with WARN on first
        use).

        Default: WARN + ``False``.
        """
        _log.warning(
            "close_topic: platform %s does not implement close_topic",
            type(self).__name__,
        )
        return False

    async def archive_topic(
        self,
        channel_id: Any,
        display_name: str,
    ) -> bool:
        """Atomic-ish: rename to ``[Archived] <display_name>`` then close.

        Used by the spec-006 ``delete_task`` lifecycle op so a deleted
        task's history remains readable. Default implementation composes
        :meth:`edit_topic_title` + :meth:`close_topic` — adapters can
        override for a single-call primitive on their platform.

        Returns ``True`` only if BOTH operations succeeded.
        """
        new_title = "[Archived] %s" % display_name
        renamed = await self.edit_topic_title(channel_id, new_title)
        closed = await self.close_topic(channel_id)
        return bool(renamed and closed)
