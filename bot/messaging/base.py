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
        """Reply to a specific message. Returns an opaque reference to the sent message."""

    @abc.abstractmethod
    async def edit_message(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> None:
        """Edit a previously sent message identified by *msg_ref*."""

    @abc.abstractmethod
    async def send_message(
        self,
        chat_id: Any,
        text: str,
        thread_id: Any = None,
        parse_mode: str | None = None,
    ) -> Any:
        """Send a new message to a chat/channel. Returns an opaque message reference."""

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
        """Send a message to a specific channel/topic."""

    async def get_invite_link(self, chat_id: Any) -> str | None:
        """Generate or retrieve an invite link for a chat/group.

        Default: not supported (returns None). Adapters should override
        for platforms that support invite links.
        """
        return None

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
