"""Robyx — Discord adapter implementing the Platform interface."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from messaging.base import Platform, retry_send

log = logging.getLogger("robyx.platform.discord")

# Discord-hosted domains. Any attachment URL the bot downloads MUST live
# under one of these; otherwise a crafted Location header or hostile
# event payload could point the bot at an attacker-controlled host.
_DISCORD_HOSTS = (
    "discord.com",
    "discordapp.com",
    "discordapp.net",   # media CDN
)

# Upper bound for single-attachment downloads. Voice memos are typically
# well under 1 MB; documents and images are capped by Discord at 25 MB
# for non-Nitro users. A 25 MB cap rejects a hostile redirect to a huge
# payload before memory is exhausted.
_MAX_DISCORD_DOWNLOAD_BYTES = 25 * 1024 * 1024


def _validate_discord_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is an HTTPS Discord-hosted URL.

    Applied to every HTTP fetch the adapter performs, not only voice
    downloads — so any future download path inherits the same guard.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Refusing non-HTTPS Discord URL")
    hostname = (parsed.hostname or "").lower()
    if not any(hostname == h or hostname.endswith("." + h) for h in _DISCORD_HOSTS):
        raise ValueError("Refusing download from non-Discord host: %s" % hostname)


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
    def bot_user_id(self) -> int | None:
        """Discord bot user id, available after ``on_ready`` has fired.

        Spec 007: used by lifecycle dispatch and by future code that
        needs to filter "is this me?" events. Returns ``None`` before
        the client has logged in.
        """
        if self._client is None:
            return None
        user = getattr(self._client, "user", None)
        return getattr(user, "id", None) if user is not None else None

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
        directly as *file_id* and download it via aiohttp.

        Security:

        * ``_validate_discord_url`` enforces HTTPS + a Discord-host
          allow-list before any network request (finding Pass 1 S3,
          generalized in Pass 2 DC-4).
        * The response body is **streamed** with a hard
          ``_MAX_DISCORD_DOWNLOAD_BYTES`` ceiling rather than loaded into
          memory in one shot, so a hostile redirect to a huge payload
          cannot exhaust the process heap (Pass 2 DC-3).
        """
        import aiohttp

        _validate_discord_url(file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file_id) as resp:
                    resp.raise_for_status()

                    # Reject up-front if the Content-Length advertises more
                    # than our cap — avoids reading any bytes at all. A
                    # malformed header (non-integer) is treated as absent;
                    # the streaming guard below catches it.
                    content_length = resp.headers.get("Content-Length")
                    declared = None
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError:
                            declared = None
                    if declared is not None and declared > _MAX_DISCORD_DOWNLOAD_BYTES:
                        raise ValueError(
                            "Discord attachment exceeds %d-byte cap"
                            % _MAX_DISCORD_DOWNLOAD_BYTES
                        )

                    # Stream; abort if running total exceeds the cap.
                    total = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > _MAX_DISCORD_DOWNLOAD_BYTES:
                                raise ValueError(
                                    "Discord attachment exceeds %d-byte cap"
                                    % _MAX_DISCORD_DOWNLOAD_BYTES
                                )
                            f.write(chunk)
        except Exception:
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

    async def leave_chat(self, chat_id: Any) -> None:
        """Spec 007: leave a Discord guild. ``chat_id`` is the canonical
        ``"<guild>:<channel>"`` form; only the guild half is used (Discord
        has no per-channel leave — the bot is a member of the whole
        guild). The handler-side shared-guild policy MUST run BEFORE
        calling this method — see ``bot/handlers.py:collab_bot_added`` and
        ``CollabStore.find_active_in_guild``.

        Idempotent at the operational level: if the guild is already gone
        from the bot's cache (kicked, deleted), ``get_guild`` returns
        ``None`` and ``fetch_guild`` raises ``discord.NotFound`` — both
        are treated as "already left, nothing to do".
        """
        import discord
        from collaborative import parse_discord_chat_id

        try:
            guild_id, _ = parse_discord_chat_id(str(chat_id))
        except ValueError as exc:
            log.warning(
                "discord.leave_chat: malformed chat_id %r — refusing: %s",
                chat_id, exc,
            )
            return

        if self._client is None:
            log.warning(
                "discord.leave_chat: client not set (called before on_ready?)",
            )
            return

        guild = self._client.get_guild(guild_id)
        if guild is None:
            try:
                guild = await self._client.fetch_guild(guild_id)
            except discord.NotFound:
                log.info(
                    "discord.leave_chat: guild %d already gone — no-op",
                    guild_id,
                )
                return
            except Exception as exc:
                log.warning(
                    "discord.leave_chat: fetch_guild %d failed: %s",
                    guild_id, exc,
                )
                return
        try:
            await guild.leave()
            log.info("discord.leave_chat: left guild %d", guild_id)
        except Exception as exc:
            log.error(
                "discord.leave_chat: guild.leave() raised for %d: %s",
                guild_id, exc,
            )

    async def get_invite_link(self, chat_id: Any) -> str | None:
        """Spec 007: generate a channel-scoped invite for the given Discord
        chat. ``chat_id`` is the canonical ``"<guild>:<channel>"`` form;
        the invite is created on the channel half.

        TTL and usage caps come from the env knobs
        :data:`config.DISCORD_INVITE_TTL_DAYS` and
        :data:`config.DISCORD_INVITE_MAX_USES`. ``0`` is the operator
        sentinel for "no limit" per Discord's ``create_invite`` API.

        Returns the invite URL (``"https://discord.gg/..."``) on success,
        or ``None`` if the channel is unreachable or the API refused
        (missing ``Create Instant Invite`` permission, channel deleted,
        etc.).
        """
        from collaborative import parse_discord_chat_id
        from .base import TopicUnreachable

        try:
            _, channel_id = parse_discord_chat_id(str(chat_id))
        except ValueError as exc:
            log.warning(
                "discord.get_invite_link: malformed chat_id %r: %s",
                chat_id, exc,
            )
            return None

        try:
            channel = await self._fetch_channel(channel_id)
        except TopicUnreachable:
            log.warning(
                "discord.get_invite_link: channel %d is unreachable",
                channel_id,
            )
            return None
        if channel is None:
            return None

        # Pull config lazily so tests can monkeypatch the values without
        # re-importing the adapter module.
        from config import DISCORD_INVITE_TTL_DAYS, DISCORD_INVITE_MAX_USES

        try:
            invite = await channel.create_invite(
                max_age=DISCORD_INVITE_TTL_DAYS * 86400,
                max_uses=DISCORD_INVITE_MAX_USES,
                reason="Robyx collaborative workspace invite",
            )
        except Exception as exc:
            log.warning(
                "discord.get_invite_link: create_invite on %d failed: %s",
                channel_id, exc,
            )
            return None
        return str(invite)

    # ── Spec 006 — dedicated-topic operations ──────────────────────────

    async def _fetch_channel(self, channel_id: int):
        """Fetch a channel, raising TopicUnreachable on 404."""
        import discord
        from .base import TopicUnreachable

        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self._client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise TopicUnreachable(channel_id, reason=str(exc))
        except Exception as exc:
            log.error("Error fetching channel %d: %s", channel_id, exc)
            return None

    async def edit_topic_title(self, channel_id: int, new_title: str) -> bool:
        """Rename a thread or channel to ``new_title``."""
        from .base import TopicUnreachable
        try:
            channel = await self._fetch_channel(channel_id)
        except TopicUnreachable:
            raise
        if channel is None:
            return False
        try:
            current = getattr(channel, "name", None)
            if current == new_title:
                return True
            await channel.edit(name=new_title)
            log.info("Renamed Discord channel %d → %r", channel_id, new_title)
            return True
        except Exception as exc:
            log.error(
                "Failed to rename Discord channel %d: %s", channel_id, exc,
            )
            return False

    async def pin_message(
        self,
        chat_id: Any,
        thread_id: int,
        message_id: int,
    ) -> bool:
        """Pin a specific message inside a channel or thread.

        Discord pins are per-channel/thread, matching Telegram semantics.
        """
        import discord
        from .base import TopicUnreachable
        try:
            channel = await self._fetch_channel(thread_id)
        except TopicUnreachable:
            raise
        if channel is None:
            return False
        try:
            msg = await channel.fetch_message(message_id)
            await msg.pin()
            log.info(
                "Pinned Discord message %d in channel %d",
                message_id, thread_id,
            )
            return True
        except discord.NotFound:
            log.warning("Cannot pin Discord message %d: not found", message_id)
            return False
        except Exception as exc:
            log.error("Error pinning Discord message %d: %s", message_id, exc)
            return False

    async def unpin_message(
        self,
        chat_id: Any,
        thread_id: int,
        message_id: int | None = None,
    ) -> bool:
        """Unpin a specific message or all pins in a Discord channel/thread."""
        import discord
        from .base import TopicUnreachable
        try:
            channel = await self._fetch_channel(thread_id)
        except TopicUnreachable:
            raise
        if channel is None:
            return False
        try:
            if message_id is None:
                pins = await channel.pins()
                for pin in pins:
                    await pin.unpin()
                log.info("Unpinned all messages in Discord channel %d", thread_id)
                return True
            msg = await channel.fetch_message(message_id)
            await msg.unpin()
            log.info(
                "Unpinned Discord message %d in channel %d",
                message_id, thread_id,
            )
            return True
        except discord.NotFound:
            log.warning(
                "Cannot unpin Discord message %s: not found", message_id,
            )
            return False
        except Exception as exc:
            log.error(
                "Error unpinning in Discord channel %d: %s", thread_id, exc,
            )
            return False

    async def close_topic(self, channel_id: int) -> bool:
        """Close (archive + lock) a Discord thread. For regular channels,
        falls back to `close_channel` which deletes the channel.
        """
        import discord
        from .base import TopicUnreachable
        try:
            channel = await self._fetch_channel(channel_id)
        except TopicUnreachable:
            raise
        if channel is None:
            return False
        try:
            if isinstance(channel, discord.Thread) or hasattr(channel, "archived"):
                await channel.edit(archived=True, locked=True)
                log.info(
                    "Closed (archived+locked) Discord thread %d", channel_id,
                )
                return True
            # Regular channels cannot be "closed" without deleting — fall
            # back to existing close_channel semantics for compatibility.
            return await self.close_channel(channel_id)
        except Exception as exc:
            log.error("Failed to close Discord channel %d: %s", channel_id, exc)
            return False

    # ── Spec 007 — collaborative-workspace lifecycle plumbing ──────────

    # Backoff sequence between audit-log retries. Exposed as a class-level
    # tuple so tests can monkeypatch a faster schedule.
    _AUDIT_LOG_BACKOFF = (1.0, 2.0, 4.0)

    async def _resolve_inviter(self, guild) -> tuple[int | None, str | None]:
        """Resolve "who added the bot" by scanning the guild's audit log.

        Discord's gateway does not carry the inviter on ``on_guild_join``
        (a deliberate API choice). The bot must look up the most recent
        ``bot_add`` entry. The lookup retries with exponential backoff
        (1s / 2s / 4s) to absorb Discord's occasional write lag between
        the join event and the audit-log entry.

        Returns ``(user_id, username)`` on success, or ``(None, None)``
        on:

        - ``discord.Forbidden`` — bot lacks the ``View Audit Log``
          permission (fail-closed, no retries).
        - Empty audit log across all retries.
        - Sustained transient errors across all retries.

        See ``specs/007-discord-parity/contracts/discord-audit-log.md``.
        """
        import asyncio
        import discord

        last_error: Exception | None = None
        attempts = len(self._AUDIT_LOG_BACKOFF)
        for attempt in range(1, attempts + 1):
            try:
                entries = [
                    e async for e in guild.audit_logs(
                        action=discord.AuditLogAction.bot_add, limit=5,
                    )
                ]
            except discord.Forbidden:
                log.warning(
                    "collab.discord.audit_log.forbidden guild=%s",
                    getattr(guild, "id", None),
                )
                return None, None
            except Exception as exc:  # noqa: BLE001 - we want any error to retry
                last_error = exc
                log.warning(
                    "collab.discord.audit_log.error guild=%s attempt=%d err=%r",
                    getattr(guild, "id", None), attempt, exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(self._AUDIT_LOG_BACKOFF[attempt - 1])
                continue

            if entries:
                user = entries[0].user
                user_id = getattr(user, "id", None) if user is not None else None
                user_name = getattr(user, "name", None) if user is not None else None
                log.info(
                    "collab.discord.audit_log.success guild=%s user=%s",
                    getattr(guild, "id", None), user_id,
                )
                return user_id, user_name

            # Empty result — retry within budget.
            if attempt < attempts:
                await asyncio.sleep(self._AUDIT_LOG_BACKOFF[attempt - 1])

        if last_error is not None:
            log.info(
                "collab.discord.audit_log.exhausted guild=%s last_error=%r",
                getattr(guild, "id", None), last_error,
            )
        else:
            log.info(
                "collab.discord.audit_log.empty guild=%s attempts=%d",
                getattr(guild, "id", None), attempts,
            )
        return None, None

    def _pick_writable_channel(self, guild):
        """Return the first text channel in ``guild`` where the bot can
        post, or ``None`` if no such channel exists.

        Preference order (matches the existing Telegram-only Discord
        unsupported-platform notice path in ``bot/bot.py`` so the
        behaviour is familiar):

        1. ``guild.system_channel`` if writable.
        2. The first entry of ``guild.text_channels`` that is writable.

        "Writable" means
        ``channel.permissions_for(guild.me).send_messages is True``.
        """
        me = getattr(guild, "me", None)
        if me is None:
            return None

        def _writable(ch) -> bool:
            try:
                perms = ch.permissions_for(me)
            except Exception:
                return False
            return bool(getattr(perms, "send_messages", False))

        system = getattr(guild, "system_channel", None)
        if system is not None and _writable(system):
            return system
        for ch in getattr(guild, "text_channels", []) or []:
            if _writable(ch):
                return ch
        return None

    def register_lifecycle(self, client) -> None:
        """Register the Discord-native ``on_guild_join`` /
        ``on_guild_remove`` handlers that translate Discord events into
        :class:`bot.messaging.base.LifecycleAdded` /
        :class:`LifecycleRemoved` and dispatch them through
        ``self.on_added`` / ``self.on_removed``.

        Called once from ``bot.bot._run_discord`` after the client is
        constructed and ``self.set_bot(client)`` has been invoked. Idempotent
        with respect to multiple calls — discord.py replaces handlers of
        the same name on re-registration.

        The Discord adapter does NOT emit :class:`LifecycleMigrated` —
        Discord has no supergroup-migration equivalent. ``self.on_migrated``
        therefore stays ``None`` for Discord guilds.
        """
        from collaborative import make_discord_chat_id
        from .base import ChatRef, LifecycleAdded, LifecycleRemoved

        @client.event
        async def on_guild_join(guild):
            inviter_id, inviter_name = await self._resolve_inviter(guild)
            channel = self._pick_writable_channel(guild)
            if channel is None:
                log.warning(
                    "collab.discord.no_writable_channel guild=%s name=%r — leaving",
                    getattr(guild, "id", None), getattr(guild, "name", None),
                )
                try:
                    await guild.leave()
                except Exception as exc:
                    log.error(
                        "collab.discord.leave_failed guild=%s err=%r",
                        getattr(guild, "id", None), exc,
                    )
                return

            if self.on_added is None:
                # No handler wired — adapter is operating without the
                # collaborative-workspace handler. Nothing to do.
                return

            event = LifecycleAdded(
                chat_ref=ChatRef(
                    platform="discord",
                    chat_id=make_discord_chat_id(guild.id, channel.id),
                ),
                chat_title=getattr(guild, "name", None),
                added_by_id=inviter_id,
                added_by_name=inviter_name,
                raw_event=guild,
            )
            try:
                await self.on_added(event)
            except Exception as exc:
                log.error(
                    "collab.discord.on_added handler raised: %r", exc,
                    exc_info=True,
                )

        @client.event
        async def on_guild_remove(guild):
            if self.on_removed is None:
                return
            # We do not know which specific channel triggered the removal
            # at the gateway level — the bot leaves the guild atomically.
            # The handler iterates ``find_active_in_guild(guild.id)`` and
            # closes every matching workspace. ``chat_id`` carries
            # ``"<guild>:0"`` as a sentinel that callers ignore.
            event = LifecycleRemoved(
                chat_ref=ChatRef(
                    platform="discord",
                    chat_id=make_discord_chat_id(guild.id, 0),
                ),
                chat_title=getattr(guild, "name", None),
                raw_event=guild,
            )
            try:
                await self.on_removed(event)
            except Exception as exc:
                log.error(
                    "collab.discord.on_removed handler raised: %r", exc,
                    exc_info=True,
                )
