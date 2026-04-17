"""Robyx — Slack adapter implementing the Platform interface."""

from __future__ import annotations

import logging
import re
import tempfile
from typing import Any

import httpx

from messaging.base import Platform, retry_send

log = logging.getLogger("robyx.platform.slack")

# Slack-hosted file origins. The bot token MUST NOT be sent to any other host.
_SLACK_FILE_HOSTS = (
    "files.slack.com",
    "files.slack-edge.com",
    "slack-files.com",
)


def _validate_slack_file_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is an HTTPS Slack-hosted file URL.

    Guards :meth:`SlackPlatform.download_voice` against token exfiltration
    via crafted event payloads or 3xx redirects to attacker-controlled hosts.
    Slack file URLs are always HTTPS and always under one of the well-known
    Slack CDN hostnames; any deviation is a red flag.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Refusing non-HTTPS Slack file URL")
    hostname = (parsed.hostname or "").lower()
    if not any(hostname == h or hostname.endswith("." + h) for h in _SLACK_FILE_HOSTS):
        raise ValueError("Refusing download from non-Slack host: %s" % hostname)


def _channel_slug(name: str) -> str:
    """Return a Slack-safe public channel name."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or "workspace")[:80]


class SlackPlatform(Platform):
    """Slack implementation of the Platform interface.

    Uses the Slack Web API via ``slack-sdk``.  The bot client is injected
    after construction via :meth:`set_bot`.

    Slack-specific conventions
    -------------------------
    * ``chat_id`` maps to a Slack **channel ID** (e.g. ``C01234ABCDE``).
    * ``thread_id`` maps to Slack's ``thread_ts`` (a timestamp string
      stored as an int-encoded representation — but in practice callers
      pass the raw ``thread_ts`` string or ``None``).
    * ``msg_ref`` is a ``dict`` with keys ``channel`` and ``ts``.
    """

    def __init__(self, bot_token: str, channel_id: str, owner_id: str):
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._owner_id = owner_id
        self._client = None  # set via set_bot()

    # ------------------------------------------------------------------
    # Platform properties
    # ------------------------------------------------------------------

    @property
    def max_message_length(self) -> int:
        return 4000

    @property
    def control_room_id(self) -> str:
        """Return the raw Slack channel ID for the control room."""
        return self._channel_id

    @property
    def control_room_channel(self) -> str:
        """The actual Slack channel ID string for the control room."""
        return self._channel_id

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    def is_owner(self, user_id: int) -> bool:
        """Compare user_id with the stored owner ID.

        Slack user IDs are strings (e.g. ``U01234``).  We compare
        string representations to tolerate both int and str inputs.
        """
        return str(user_id) == str(self._owner_id)

    def is_main_thread(self, chat_id, thread_id) -> bool:
        # On Slack, Robyx lives at the top level of the control-room channel.
        # Any ``thread_ts`` means the user is inside a Slack thread.
        return thread_id is None and str(chat_id) == str(self._channel_id)

    # ------------------------------------------------------------------
    # Bot injection
    # ------------------------------------------------------------------

    def set_bot(self, client) -> None:
        """Receive the Slack ``AsyncWebClient`` instance."""
        self._client = client

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        thread_id: Any = None,
        parse_mode: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"channel": str(chat_id), "text": text}
        if thread_id is not None:
            kwargs["thread_ts"] = str(thread_id)
        resp = await retry_send(
            lambda: self._client.chat_postMessage(**kwargs),
            label="slack.send_message",
        )
        return {"channel": resp["channel"], "ts": resp["ts"]}

    async def reply(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> Any:
        """Reply in the same thread as ``msg_ref``.

        ``msg_ref`` is a dict with ``channel`` and ``ts``.
        """
        resp = await self._client.chat_postMessage(
            channel=msg_ref["channel"],
            text=text,
            thread_ts=msg_ref["ts"],
        )
        return {"channel": resp["channel"], "ts": resp["ts"]}

    async def edit_message(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> None:
        await self._client.chat_update(
            channel=msg_ref["channel"],
            ts=msg_ref["ts"],
            text=text,
        )

    async def send_typing(self, chat_id: Any, thread_id: Any = None) -> None:
        """Slack does not support typing indicators for bots — no-op."""
        log.debug("send_typing called (no-op on Slack)")

    async def send_photo(
        self,
        chat_id: Any,
        path: str,
        caption: str | None = None,
        thread_id: Any = None,
    ) -> Any:
        import os
        from media import prepare_image_for_upload, MediaError

        try:
            prepared = prepare_image_for_upload(path, self.max_photo_bytes)
        except MediaError as e:
            log.error("send_photo: media prep failed for %s: %s", path, e)
            return None

        cleanup = prepared != path
        kwargs: dict[str, Any] = {
            "channel": str(chat_id),
            "file": prepared,
        }
        if caption:
            kwargs["initial_comment"] = caption
        if thread_id is not None:
            kwargs["thread_ts"] = str(thread_id)
        try:
            return await self._client.files_upload_v2(**kwargs)
        except Exception as e:
            log.error("send_photo: upload failed for %s: %s", path, e)
            return None
        finally:
            if cleanup:
                try:
                    os.unlink(prepared)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Voice / files
    # ------------------------------------------------------------------

    async def download_voice(self, file_id: str) -> str:
        """Download a Slack audio file.

        ``file_id`` is the ``url_private_download`` URL of the file.
        The bot token is used as a Bearer token for authentication.

        Security: the URL is validated against an allow-list of Slack-hosted
        hostnames before each request, and 3xx redirects are followed
        manually so that the bearer token is never sent to a non-Slack host
        (httpx's ``follow_redirects=True`` would forward Authorization
        verbatim on cross-host redirects, enabling token exfiltration via
        a crafted Location header).
        """
        _validate_slack_file_url(file_id)
        headers = {"Authorization": "Bearer %s" % self._bot_token}
        current_url = file_id
        max_hops = 5
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
            while True:
                resp = await http.get(current_url, headers=headers)
                if resp.is_redirect and max_hops > 0:
                    location = resp.headers.get("location", "")
                    if not location:
                        break
                    if location.startswith("/") or not location.startswith("http"):
                        from urllib.parse import urljoin
                        location = urljoin(str(resp.url), location)
                    _validate_slack_file_url(location)
                    current_url = location
                    max_hops -= 1
                    continue
                break
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp.write(resp.content)
                return tmp.name

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    async def create_channel(self, name: str) -> str | None:
        try:
            slug = _channel_slug(name)
            resp = await self._client.conversations_create(name=slug, is_private=False)
            channel_id = resp["channel"]["id"]
            log.info("Created Slack channel '%s' as #%s (id=%s)", name, slug, channel_id)
            return channel_id
        except Exception as e:
            log.error("Failed to create channel '%s': %s", name, e)
            return None

    async def close_channel(self, channel_id: Any) -> bool:
        try:
            await self._client.conversations_archive(channel=str(channel_id))
            log.info("Archived channel (id=%s)", channel_id)
            return True
        except Exception as e:
            log.error("Failed to archive channel: %s", e)
            return False

    async def rename_main_channel(self, display_name: str, slug: str) -> bool:
        """Rename the control-room channel on Slack.

        Slack channel names must be lowercase with no spaces (hyphens,
        underscores and periods are allowed), so the ``slug`` form is used.
        Idempotent: reads ``conversations.info`` first and skips the
        rename if the channel is already named correctly. Requires the
        ``channels:manage`` scope for public channels (or equivalent for
        private channels).
        """
        try:
            info = await self._client.conversations_info(channel=self._channel_id)
            current = info.get("channel", {}).get("name", "")
            if current == slug:
                log.info("Slack control channel already named %r", slug)
                return True
            await self._client.conversations_rename(
                channel=self._channel_id,
                name=slug,
            )
            log.info("Renamed Slack control channel %r → %r", current, slug)
            return True
        except Exception as e:
            log.error("Failed to rename Slack control channel: %s", e)
            return False

    async def send_to_channel(self, channel_id: Any, text: str, parse_mode: str | None = None) -> bool:
        try:
            await self._client.chat_postMessage(channel=str(channel_id), text=text)
            return True
        except Exception as e:
            log.error("Error sending to channel %s: %s", channel_id, e)
            return False

    async def leave_chat(self, chat_id: Any) -> None:
        raise NotImplementedError(
            "leave_chat is not yet supported on Slack — external collaborative "
            "groups are Telegram-only in this iteration"
        )
