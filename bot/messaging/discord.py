"""Robyx — Discord adapter implementing the Platform interface."""

from __future__ import annotations

import logging
import tempfile
from typing import Any

from messaging.base import Platform, retry_send

log = logging.getLogger("robyx.platform.discord")


class DiscordPlatform(Platform):
    """Discord implementation of the Platform interface."""

    def __init__(
        self,
        bot_token: str,
        guild_id: int | None,
        owner_id: int,
        control_channel_id: int | None,
    ):
        self._bot_token = bot_token
        self._guild_id = guild_id
        self._owner_id = owner_id
        self._control_channel_id = control_channel_id
        self._client = None  # set via set_bot()

    def set_bot(self, client) -> None:
        """Set the discord.Client instance (called during app setup)."""
        self._client = client

    @property
    def max_message_length(self) -> int:
        return 2000

    @property
    def max_photo_bytes(self) -> int:
        # Discord free-tier upload cap is 8 MiB. Servers with Nitro boosts
        # can go higher (25 MB / 100 MB / 500 MB). Use the conservative
        # baseline — re-encoding to fit 8 MB always works for boosted
        # servers too.
        return 8 * 1024 * 1024

    @property
    def control_room_id(self) -> int:
        return self._control_channel_id or 0

    def is_owner(self, user_id: int) -> bool:
        return user_id == self._owner_id

    def is_main_thread(self, chat_id, thread_id) -> bool:
        # On Discord, Robyx lives in the configured control channel.
        # ``thread_id`` carries the channel id (see on_message handler).
        if self._control_channel_id is None:
            return False
        return thread_id == self._control_channel_id

    async def reply(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> Any:
        """msg_ref is a discord.Message object."""
        return await msg_ref.reply(text)

    async def edit_message(self, msg_ref: Any, text: str, parse_mode: str | None = None) -> None:
        """Edit a previously sent message."""
        await msg_ref.edit(content=text)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        """Send a new message to a channel or thread."""
        # If thread_id is given, send to the thread; otherwise send to channel
        target_id = thread_id if thread_id is not None else chat_id
        channel = self._client.get_channel(target_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(target_id)
            except Exception:
                log.error("Could not find channel %d", target_id)
                return None
        return await retry_send(
            lambda: channel.send(text), label="discord.send_message",
        )

    async def send_typing(self, chat_id: int, thread_id: int | None = None) -> None:
        """Send a typing indicator to a channel."""
        target_id = thread_id if thread_id is not None else chat_id
        channel = self._client.get_channel(target_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(target_id)
            except Exception:
                log.error("Could not find channel %d for typing", target_id)
                return
        await channel.typing()

    async def send_photo(
        self,
        chat_id: int,
        path: str,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> Any:
        import os
        import discord
        from media import prepare_image_for_upload, MediaError

        try:
            prepared = prepare_image_for_upload(path, self.max_photo_bytes)
        except MediaError as e:
            log.error("send_photo: media prep failed for %s: %s", path, e)
            return None

        cleanup = prepared != path
        target_id = thread_id if thread_id is not None else chat_id
        channel = self._client.get_channel(target_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(target_id)
            except Exception:
                log.error("Could not find channel %d for photo", target_id)
                if cleanup:
                    try:
                        os.unlink(prepared)
                    except OSError:
                        pass
                return None
        try:
            return await channel.send(
                content=caption or None,
                file=discord.File(prepared),
            )
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
        """Download a voice attachment and return the local temp path.

        For Discord, file_id is formatted as ``<message_id>:<attachment_index>``
        by the event handler, but the actual download is done by passing the
        attachment URL.  To keep things simple, we accept the attachment URL
        directly as *file_id* and download it via the discord.py HTTP session.
        """
        import aiohttp
        from urllib.parse import urlparse

        # Validate URL to prevent SSRF via crafted attachment URLs.
        parsed = urlparse(file_id)
        hostname = parsed.hostname or ""
        is_discord = hostname.endswith(".discordapp.com") or hostname.endswith(".discord.com")
        if parsed.scheme != "https" or not is_discord:
            raise ValueError("Refusing to download from non-Discord URL: %s" % file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file_id) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            with open(tmp_path, "wb") as f:
                f.write(data)
        except Exception:
            import os
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return tmp_path

    async def create_channel(self, name: str) -> int | None:
        """Create a text channel (or forum thread) in the guild.

        If a channel named ``Workspaces`` (forum type) exists, creates a thread
        inside it.  Otherwise, creates a regular text channel.
        """
        import discord

        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            try:
                guild = await self._client.fetch_guild(self._guild_id)
            except Exception:
                log.error("Could not find guild %s", self._guild_id)
                return None

        # Look for a "Workspaces" forum channel
        for ch in guild.channels:
            if isinstance(ch, discord.ForumChannel) and ch.name.lower() == "workspaces":
                try:
                    thread, _initial_msg = await ch.create_thread(
                        name=name,
                        content="Workspace created.",
                    )
                    log.info("Created forum thread '%s' (id=%d)", name, thread.id)
                    return thread.id
                except Exception as e:
                    log.error("Failed to create forum thread '%s': %s", name, e)
                    return None

        # Fallback: create a regular text channel
        try:
            channel = await guild.create_text_channel(name=name)
            log.info("Created text channel '%s' (id=%d)", name, channel.id)
            return channel.id
        except Exception as e:
            log.error("Failed to create channel '%s': %s", name, e)
            return None

    async def close_channel(self, channel_id: int) -> bool:
        """Archive a thread or delete a channel."""
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception:
                log.error("Could not find channel %d to close", channel_id)
                return False

        try:
            # If it's a thread, archive it
            if hasattr(channel, "archived"):
                await channel.edit(archived=True)
                log.info("Archived thread %d", channel_id)
                return True
            # Otherwise just delete the channel
            await channel.delete(reason="Workspace closed by Robyx")
            log.info("Deleted channel %d", channel_id)
            return True
        except Exception as e:
            log.error("Failed to close channel %d: %s", channel_id, e)
            return False

    async def rename_main_channel(self, display_name: str, slug: str) -> bool:
        """Rename the configured control channel.

        Discord channel names must be lowercase with no spaces, so the
        ``slug`` form is used (e.g. ``"headquarters"``). Requires the
        ``manage_channels`` permission on the target channel. Idempotent:
        if the channel already has the target name, returns ``True``
        without making an API call.
        """
        if self._control_channel_id is None:
            log.error("Cannot rename main channel: no control_channel_id set")
            return False

        channel = self._client.get_channel(self._control_channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(self._control_channel_id)
            except Exception as e:
                log.error("Could not fetch control channel for rename: %s", e)
                return False

        try:
            current = getattr(channel, "name", None)
            if current == slug:
                log.info("Discord control channel already named %r", slug)
                return True
            await channel.edit(name=slug, reason="Robyx migration: control channel rename")
            log.info("Renamed Discord control channel %r → %r", current, slug)
            return True
        except Exception as e:
            log.error("Failed to rename Discord control channel: %s", e)
            return False

    async def send_to_channel(self, channel_id: int, text: str, parse_mode: str | None = None) -> bool:
        """Send a message to a specific channel or thread by ID."""
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception:
                log.error("Could not find channel %d", channel_id)
                return False
        try:
            await channel.send(text)
            return True
        except Exception as e:
            log.error("Error sending to channel %d: %s", channel_id, e)
            return False
