"""Robyx — Telegram adapter implementing the Platform interface."""

from __future__ import annotations

import logging
import tempfile
from typing import Any

import httpx

from messaging.base import Platform, PlatformMessage

log = logging.getLogger("robyx.platform.telegram")


class TelegramPlatform(Platform):
    """Telegram implementation of the Platform interface."""

    def __init__(self, bot_token: str, chat_id: int, owner_id: int):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._owner_id = owner_id
        self._api_base = "https://api.telegram.org/bot%s" % bot_token

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

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("%s/sendMessage" % self._api_base, data=data)
            result = resp.json()
            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description") or "Telegram sendMessage failed"
                )
            return result.get("result")

    async def send_typing(self, chat_id: int, thread_id: int | None = None) -> None:
        data: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
        if thread_id is not None:
            data["message_thread_id"] = thread_id
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post("%s/sendChatAction" % self._api_base, data=data)

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
        voice_file = await self._bot.get_file(file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
        return tmp_path

    async def create_channel(self, name: str) -> int | None:
        data = {"chat_id": self._chat_id, "name": name}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post("%s/createForumTopic" % self._api_base, data=data)
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
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "%s/closeForumTopic" % self._api_base,
                    data={"chat_id": self._chat_id, "message_thread_id": channel_id},
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
            if parse_mode:
                data["parse_mode"] = parse_mode
            elif parse_mode is None:
                data["parse_mode"] = "Markdown"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post("%s/sendMessage" % self._api_base, data=data)
                return resp.json().get("ok", False)
        except Exception as e:
            log.error("Error sending to topic %d: %s", channel_id, e)
            return False

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

    def set_bot(self, bot):
        """Set the telegram Bot instance (called during app setup)."""
        self._bot = bot
