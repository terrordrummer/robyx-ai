"""Robyx — Telegram adapter implementing the Platform interface."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

import httpx

from messaging.base import Platform, PlatformMessage, retry_send

log = logging.getLogger("robyx.platform.telegram")

# Hard ceiling for voice-message downloads. Telegram's own Bot API cap for
# voice/audio is 50 MB, but we keep our acceptance window tight to bound
# disk impact from repeated-long-voice DoS. Mirrors discord.py's
# `_MAX_DISCORD_DOWNLOAD_BYTES`; see Pass 2 P2-82 / P2-11.
_MAX_TELEGRAM_VOICE_BYTES = 25 * 1024 * 1024


class TelegramPlatform(Platform):
    """Telegram implementation of the Platform interface."""

    def __init__(self, bot_token: str, chat_id: int, owner_id: int):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._owner_id = owner_id
        self._api_base = "https://api.telegram.org/bot%s" % bot_token
        self._bot = None  # Set via set_bot() when PTB Application is ready
        # Persistent httpx client. Creating a fresh AsyncClient per call
        # (the pre-0.20.16 behaviour) was the root cause of the
        # "typing indicator doesn't show immediately in Headquarters"
        # bug: every send_typing / send_message did a cold DNS + TCP +
        # TLS handshake, easily costing 200-500ms per call. Reusing a
        # single client lets httpx pool connections to api.telegram.org,
        # dropping the per-call latency to ~RTT.
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-create a persistent httpx client.

        We can't create it at ``__init__`` time because there is no
        running event loop yet (``__init__`` runs at import / wiring
        time). The first call from inside an async handler creates it.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
                # Keep connections to api.telegram.org alive across calls
                # so subsequent sends pay only the RTT, not the TLS
                # handshake.
                limits=httpx.Limits(
                    max_keepalive_connections=4,
                    max_connections=8,
                    keepalive_expiry=300,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the persistent client. Safe to call multiple times."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def max_message_length(self) -> int:
        return 4000

    @property
    def control_room_id(self) -> int | None:
        # Telegram's General topic of a forum supergroup is addressed with
        # ``message_thread_id=0``. The earlier hard-coded ``1`` was rejected
        # by recent Bot API versions, so scheduler / boot / update messages
        # silently failed to land on Headquarters. Returning ``0``
        # restores delivery without breaking topic-aware sends.
        return 0

    def is_owner(self, user_id: int) -> bool:
        return user_id == self._owner_id

    def is_main_thread(self, chat_id, thread_id) -> bool:
        # On Telegram the General topic of a forum supergroup has no
        # ``message_thread_id``. Any other value identifies a forum topic.
        return thread_id is None

    async def reply(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> Any:
        """msg_ref is a telegram Message object (or mock)."""
        kwargs = {}
        if parse_mode == "markdown":
            from telegram.constants import ParseMode
            kwargs["parse_mode"] = ParseMode.MARKDOWN
        return await msg_ref.reply_text(text, **kwargs)

    async def edit_message(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> None:
        kwargs = {}
        if parse_mode == "markdown":
            from telegram.constants import ParseMode
            kwargs["parse_mode"] = ParseMode.MARKDOWN
        await msg_ref.edit_text(text, **kwargs)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        # We hit the raw Bot API directly here instead of going through
        # python-telegram-bot. PTB's ``Bot.send_message`` has been
        # intermittently unreliable for control-room sends in forum chats
        # (silent drops, occasional 60-second hangs after sleep/wake), and
        # the failure mode is identical to "the bot is alive but never
        # answers". Using httpx with the same payload that direct probes
        # use makes the behaviour predictable and easy to time out.
        data: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            data["message_thread_id"] = thread_id
        if parse_mode == "markdown":
            data["parse_mode"] = "Markdown"

        async def _do_send() -> Any:
            client = self._get_client()
            resp = await client.post(
                "%s/sendMessage" % self._api_base, data=data, timeout=30,
            )
            result = resp.json()
            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description") or "Telegram sendMessage failed"
                )
            return result.get("result")

        return await retry_send(_do_send, label="telegram.send_message")

    async def send_typing(self, chat_id: int, thread_id: int | None = None) -> None:
        data: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
        if thread_id is not None:
            data["message_thread_id"] = thread_id
        client = self._get_client()
        await client.post(
            "%s/sendChatAction" % self._api_base, data=data, timeout=10,
        )

    async def send_photo(
        self,
        chat_id: int,
        path: str,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> Any:
        import os
        from media import prepare_image_for_upload, MediaError

        try:
            prepared = prepare_image_for_upload(path, self.max_photo_bytes)
        except MediaError as e:
            log.error("send_photo: media prep failed for %s: %s", path, e)
            return None

        cleanup = prepared != path
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if caption:
            kwargs["caption"] = caption
        try:
            with open(prepared, "rb") as fh:
                return await self._bot.send_photo(photo=fh, **kwargs)
        except Exception as e:
            log.error("send_photo: upload failed for %s: %s", path, e)
            return None
        finally:
            if cleanup:
                try:
                    os.unlink(prepared)
                except OSError:
                    pass

    async def download_voice(self, file_id: str) -> str:
        """Download a Telegram voice file to a local temp path.

        Security:
        * ``telegram.File.file_size`` (set by the ``getFile`` API response)
          is checked against ``_MAX_TELEGRAM_VOICE_BYTES`` BEFORE any
          bytes are read to disk — a declared-oversize file is refused
          up-front with no download attempt.
        * The temp file is unlinked on any failure (size refusal or
          download error) so rejected requests leave no on-disk residue.
        * After download, the actual file size is verified once more as
          defense-in-depth against a server lying in ``getFile`` vs the
          ``download`` endpoint. Mirrors Pass 2 P2-11 on discord.py.
        """
        voice_file = await self._bot.get_file(file_id)
        declared = getattr(voice_file, "file_size", None)
        if declared is not None and declared > _MAX_TELEGRAM_VOICE_BYTES:
            raise ValueError(
                "Telegram voice file exceeds %d-byte cap (declared=%d)"
                % (_MAX_TELEGRAM_VOICE_BYTES, declared)
            )
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await voice_file.download_to_drive(tmp_path)
            try:
                actual = os.path.getsize(tmp_path)
            except OSError:
                actual = 0
            if actual > _MAX_TELEGRAM_VOICE_BYTES:
                raise ValueError(
                    "Telegram voice file exceeds %d-byte cap (actual=%d)"
                    % (_MAX_TELEGRAM_VOICE_BYTES, actual)
                )
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return tmp_path

    async def create_channel(self, name: str) -> int | None:
        data = {"chat_id": self._chat_id, "name": name}
        try:
            client = self._get_client()
            resp = await client.post(
                "%s/createForumTopic" % self._api_base, data=data, timeout=30,
            )
            result = resp.json()
            if result.get("ok"):
                thread_id = result["result"]["message_thread_id"]
                log.info("Created topic '%s' (thread_id=%d)", name, thread_id)
                return thread_id
            log.error("Failed to create topic '%s': %s", name, result)
            return None
        except Exception as e:
            log.error("Error creating topic '%s': %s", name, e)
            return None

    async def close_channel(self, channel_id: int) -> bool:
        try:
            client = self._get_client()
            resp = await client.post(
                "%s/closeForumTopic" % self._api_base,
                data={"chat_id": self._chat_id, "message_thread_id": channel_id},
                timeout=30,
            )
            result = resp.json()
            if result.get("ok"):
                log.info("Closed topic (thread_id=%d)", channel_id)
                return True
            log.error("Failed to close topic: %s", result)
            return False
        except Exception as e:
            log.error("Error closing topic: %s", e)
            return False

    async def send_to_channel(self, channel_id: int, text: str, parse_mode: str | None = None) -> bool:
        try:
            data = {
                "chat_id": self._chat_id,
                "message_thread_id": channel_id,
                "text": text,
            }
            # Forum topics default to Markdown rendering for agent output;
            # callers can opt out with parse_mode="" (empty string) or pass
            # an explicit Telegram-native value like "HTML".
            if parse_mode is None or parse_mode == "markdown":
                data["parse_mode"] = "Markdown"
            elif parse_mode:
                data["parse_mode"] = parse_mode
            client = self._get_client()
            resp = await client.post(
                "%s/sendMessage" % self._api_base, data=data, timeout=30,
            )
            return resp.json().get("ok", False)
        except Exception as e:
            log.error("Error sending to topic %d: %s", channel_id, e)
            return False

    async def get_invite_link(self, chat_id: int) -> str | None:
        try:
            client = self._get_client()
            resp = await client.post(
                "%s/exportChatInviteLink" % self._api_base,
                data={"chat_id": chat_id},
                timeout=15,
            )
            result = resp.json()
            if result.get("ok"):
                return result["result"]
            log.warning("exportChatInviteLink failed: %s", result)
            return None
        except Exception as e:
            log.error("Error generating invite link for chat %d: %s", chat_id, e)
            return None

    async def leave_chat(self, chat_id: int) -> None:
        """Leave a Telegram chat/group. Used by the unauthorised-adder
        guard for external collaborative groups.

        Uses the PTB ``Bot.leave_chat`` API; raises on transport failure
        so the caller can log and still post the HQ notification. The
        caller is responsible for sending any user-facing "leaving" copy
        BEFORE calling this, since once we leave we can no longer post
        in the chat.
        """
        if self._bot is None:
            raise RuntimeError("Telegram bot not set; cannot leave_chat")
        await self._bot.leave_chat(chat_id=chat_id)
        log.info("Left Telegram chat %d", chat_id)

    async def rename_main_channel(self, display_name: str, slug: str) -> bool:
        """Rename the General topic of the forum supergroup.

        Uses the bot API ``editGeneralForumTopic`` which specifically
        targets the General topic by chat id (no thread_id needed — there
        is only one General per forum). The bot must have the
        ``can_manage_topics`` admin right.

        Telegram supports spaces and mixed case, so ``display_name`` is
        used verbatim. The ``slug`` argument is ignored.
        """
        try:
            await self._bot.edit_general_forum_topic(
                chat_id=self._chat_id,
                name=display_name,
            )
            log.info("Renamed Telegram General topic to %r", display_name)
            return True
        except Exception as e:
            log.error("Failed to rename Telegram General topic: %s", e)
            return False

    # ── Spec 006 — dedicated-topic operations ──────────────────────────

    _TOPIC_UNREACHABLE_MARKERS = (
        "TOPIC_ID_INVALID",
        "TOPIC_DELETED",
        "TOPIC_CLOSED",
        "MESSAGE_THREAD_NOT_FOUND",
        "chat not found",
    )

    def _is_topic_unreachable(self, result: dict) -> bool:
        """True if the Bot API error payload signals a permanently gone topic."""
        if result.get("ok"):
            return False
        desc = (result.get("description") or "").lower()
        return any(m.lower() in desc for m in self._TOPIC_UNREACHABLE_MARKERS)

    async def edit_topic_title(self, channel_id: int, new_title: str) -> bool:
        """Rename a forum topic via ``editForumTopic``.

        Raises :class:`TopicUnreachable` if Telegram reports the topic is
        gone. Returns False on transient errors (logged).
        """
        from .base import TopicUnreachable
        data = {
            "chat_id": self._chat_id,
            "message_thread_id": channel_id,
            "name": new_title,
        }
        try:
            client = self._get_client()
            resp = await client.post(
                "%s/editForumTopic" % self._api_base, data=data, timeout=30,
            )
            result = resp.json()
            if result.get("ok"):
                log.info(
                    "Edited topic title (thread_id=%d) → %r",
                    channel_id, new_title,
                )
                return True
            if self._is_topic_unreachable(result):
                raise TopicUnreachable(
                    channel_id, reason=result.get("description", ""),
                )
            log.warning(
                "editForumTopic failed for %d: %s", channel_id, result,
            )
            return False
        except TopicUnreachable:
            raise
        except Exception as exc:
            log.error(
                "Error editing topic title %d: %s", channel_id, exc,
            )
            return False

    async def pin_message(
        self,
        chat_id: int,
        thread_id: int,
        message_id: int,
    ) -> bool:
        """Pin a specific message in a forum topic via ``pinChatMessage``."""
        from .base import TopicUnreachable
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True,  # silent pin — no ping
        }
        try:
            client = self._get_client()
            resp = await client.post(
                "%s/pinChatMessage" % self._api_base, data=data, timeout=30,
            )
            result = resp.json()
            if result.get("ok"):
                log.info(
                    "Pinned message %d in topic %d", message_id, thread_id,
                )
                return True
            if self._is_topic_unreachable(result):
                raise TopicUnreachable(
                    thread_id, reason=result.get("description", ""),
                )
            log.warning("pinChatMessage failed: %s", result)
            return False
        except TopicUnreachable:
            raise
        except Exception as exc:
            log.error("Error pinning message %d: %s", message_id, exc)
            return False

    async def unpin_message(
        self,
        chat_id: int,
        thread_id: int,
        message_id: int | None = None,
    ) -> bool:
        """Unpin a specific message or all pins in a topic.

        If ``message_id`` is None, uses ``unpinAllForumTopicMessages`` to
        clear every pin in the topic in one shot.
        """
        from .base import TopicUnreachable
        try:
            client = self._get_client()
            if message_id is None:
                endpoint = "unpinAllForumTopicMessages"
                data = {
                    "chat_id": chat_id,
                    "message_thread_id": thread_id,
                }
            else:
                endpoint = "unpinChatMessage"
                data = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                }
            resp = await client.post(
                "%s/%s" % (self._api_base, endpoint),
                data=data,
                timeout=30,
            )
            result = resp.json()
            if result.get("ok"):
                log.info(
                    "Unpinned in topic %d (message_id=%s)",
                    thread_id, message_id,
                )
                return True
            if self._is_topic_unreachable(result):
                raise TopicUnreachable(
                    thread_id, reason=result.get("description", ""),
                )
            log.warning("%s failed: %s", endpoint, result)
            return False
        except TopicUnreachable:
            raise
        except Exception as exc:
            log.error(
                "Error unpinning in topic %d: %s", thread_id, exc,
            )
            return False

    async def close_topic(self, channel_id: int) -> bool:
        """Close a forum topic to new messages.

        Alias for :meth:`close_channel` with spec-006 semantics (history
        visible; no new messages accepted).
        """
        return await self.close_channel(channel_id)

    def set_bot(self, bot):
        """Set the telegram Bot instance (called during app setup)."""
        self._bot = bot

    @property
    def bot_username(self) -> str | None:
        if self._bot is None:
            return None
        return getattr(self._bot, "username", None)
