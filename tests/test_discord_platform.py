"""Tests for bot/messaging/discord.py — Discord platform adapter."""

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from messaging.discord import DiscordPlatform


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def platform():
    """A DiscordPlatform with test config."""
    return DiscordPlatform(
        bot_token="test-discord-token",
        guild_id=111222333,
        owner_id=42,
        control_channel_id=444555666,
    )


@pytest.fixture
def platform_with_client(platform):
    """Platform with a mock discord.Client set."""
    client = MagicMock()
    platform.set_bot(client)
    return platform


@pytest.fixture
def mock_channel():
    """A mock Discord channel."""
    ch = AsyncMock()
    ch.send = AsyncMock(return_value=MagicMock(id=9999))
    ch.typing = AsyncMock()
    return ch


@pytest.fixture
def mock_message():
    """A mock Discord message (msg_ref)."""
    msg = AsyncMock()
    msg.reply = AsyncMock(return_value=MagicMock(id=8888))
    msg.edit = AsyncMock()
    return msg


# ═══════════════════════════════════════════════════════════════════════════
# Properties
# ═══════════════════════════════════════════════════════════════════════════


class TestProperties:
    def test_max_message_length(self, platform):
        assert platform.max_message_length == 2000

    def test_control_room_id(self, platform):
        assert platform.control_room_id == 444555666

    def test_control_room_id_none(self):
        p = DiscordPlatform("token", 111, 42, None)
        assert p.control_room_id == 0


# ═══════════════════════════════════════════════════════════════════════════
# is_owner
# ═══════════════════════════════════════════════════════════════════════════


class TestIsOwner:
    def test_owner_matches(self, platform):
        assert platform.is_owner(42) is True

    def test_owner_no_match(self, platform):
        assert platform.is_owner(99) is False


class TestSendPhoto:
    @pytest.mark.asyncio
    async def test_send_photo_in_channel(self, platform_with_client, mock_channel, tmp_path):
        from PIL import Image
        img_path = tmp_path / "ok.png"
        Image.new("RGB", (32, 32), color=(0, 128, 255)).save(img_path, "PNG")

        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        result = await platform_with_client.send_photo(
            chat_id=111222333,
            path=str(img_path),
            caption="test caption",
            thread_id=None,
        )

        mock_channel.send.assert_awaited_once()
        kwargs = mock_channel.send.call_args[1]
        assert kwargs["content"] == "test caption"
        assert kwargs["file"] is not None
        assert result is not None

    @pytest.mark.asyncio
    async def test_send_photo_missing_file_returns_none(self, platform_with_client, tmp_path):
        result = await platform_with_client.send_photo(
            chat_id=111222333,
            path=str(tmp_path / "nope.png"),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_send_photo_in_thread(self, platform_with_client, mock_channel, tmp_path):
        from PIL import Image
        img_path = tmp_path / "threaded.png"
        Image.new("RGB", (32, 32), color=(0, 0, 0)).save(img_path, "PNG")

        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        await platform_with_client.send_photo(
            chat_id=111222333,
            path=str(img_path),
            thread_id=9999999999,
        )
        platform_with_client._client.get_channel.assert_called_with(9999999999)
        mock_channel.send.assert_awaited_once()


class TestMaxPhotoBytes:
    def test_discord_photo_cap_is_8mb(self, platform):
        assert platform.max_photo_bytes == 8 * 1024 * 1024


class TestRenameMainChannel:
    @pytest.mark.asyncio
    async def test_renames_control_channel(self, platform_with_client):
        channel = AsyncMock()
        channel.name = "old-name"
        channel.edit = AsyncMock()
        platform_with_client._client.get_channel = MagicMock(return_value=channel)

        ok = await platform_with_client.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )

        assert ok is True
        channel.edit.assert_awaited_once()
        assert channel.edit.call_args[1]["name"] == "command-bridge"

    @pytest.mark.asyncio
    async def test_idempotent_when_already_named(self, platform_with_client):
        channel = AsyncMock()
        channel.name = "command-bridge"
        channel.edit = AsyncMock()
        platform_with_client._client.get_channel = MagicMock(return_value=channel)

        ok = await platform_with_client.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )

        assert ok is True
        channel.edit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_control_channel_id(self):
        p = DiscordPlatform("token", 111, 42, None)
        client = MagicMock()
        p.set_bot(client)
        ok = await p.rename_main_channel("Command Bridge", "command-bridge")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_on_edit_exception(self, platform_with_client):
        channel = AsyncMock()
        channel.name = "old"
        channel.edit = AsyncMock(side_effect=Exception("missing perms"))
        platform_with_client._client.get_channel = MagicMock(return_value=channel)

        ok = await platform_with_client.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )
        assert ok is False


class TestIsMainThread:
    def test_control_channel_is_main(self, platform):
        # Messages arriving from the configured control channel are "main".
        assert platform.is_main_thread(111222333, 444555666) is True

    def test_other_channel_is_not_main(self, platform):
        assert platform.is_main_thread(111222333, 999000111) is False

    def test_none_thread_is_not_main(self, platform):
        # Discord never delivers None thread ids in the real event handler,
        # but the helper must still return False for sanity.
        assert platform.is_main_thread(111222333, None) is False

    def test_no_control_channel_configured_is_never_main(self):
        p = DiscordPlatform("token", 111, 42, None)
        assert p.is_main_thread(111, 444555666) is False


# ═══════════════════════════════════════════════════════════════════════════
# reply
# ═══════════════════════════════════════════════════════════════════════════


class TestReply:
    @pytest.mark.asyncio
    async def test_reply_calls_msg_ref(self, platform, mock_message):
        result = await platform.reply(mock_message, "hello")
        mock_message.reply.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_reply_returns_sent_message(self, platform, mock_message):
        result = await platform.reply(mock_message, "hello")
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# edit_message
# ═══════════════════════════════════════════════════════════════════════════


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edit_message(self, platform, mock_message):
        await platform.edit_message(mock_message, "updated text")
        mock_message.edit.assert_awaited_once_with(content="updated text")


# ═══════════════════════════════════════════════════════════════════════════
# send_message
# ═══════════════════════════════════════════════════════════════════════════


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_to_channel(self, platform_with_client, mock_channel):
        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        result = await platform_with_client.send_message(chat_id=100, text="hello")
        mock_channel.send.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_send_message_with_thread_id(self, platform_with_client, mock_channel):
        """When thread_id is given, send to the thread instead of chat_id."""
        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        await platform_with_client.send_message(chat_id=100, text="hi", thread_id=200)
        # get_channel should be called with thread_id (200), not chat_id (100)
        platform_with_client._client.get_channel.assert_called_once_with(200)

    @pytest.mark.asyncio
    async def test_send_message_fetch_fallback(self, platform_with_client, mock_channel):
        """If get_channel returns None, fall back to fetch_channel."""
        platform_with_client._client.get_channel = MagicMock(return_value=None)
        platform_with_client._client.fetch_channel = AsyncMock(return_value=mock_channel)
        result = await platform_with_client.send_message(chat_id=100, text="fallback")
        platform_with_client._client.fetch_channel.assert_awaited_once_with(100)
        mock_channel.send.assert_awaited_once_with("fallback")

    @pytest.mark.asyncio
    async def test_send_message_channel_not_found(self, platform_with_client):
        """Returns None when channel cannot be found."""
        platform_with_client._client.get_channel = MagicMock(return_value=None)
        platform_with_client._client.fetch_channel = AsyncMock(side_effect=Exception("not found"))
        result = await platform_with_client.send_message(chat_id=100, text="nope")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# send_typing
# ═══════════════════════════════════════════════════════════════════════════


class TestSendTyping:
    @pytest.mark.asyncio
    async def test_send_typing(self, platform_with_client, mock_channel):
        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        await platform_with_client.send_typing(chat_id=100)
        mock_channel.typing.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_typing_with_thread_id(self, platform_with_client, mock_channel):
        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        await platform_with_client.send_typing(chat_id=100, thread_id=200)
        platform_with_client._client.get_channel.assert_called_once_with(200)


# ═══════════════════════════════════════════════════════════════════════════
# download_voice
# ═══════════════════════════════════════════════════════════════════════════


def _make_aiohttp_mock(chunks: list[bytes], content_length: str | None = None):
    """Build a mock ``aiohttp`` module whose ClientSession().get() yields
    a response with the given chunked body and optional Content-Length."""

    class _ChunkIter:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop(0)

    class _Content:
        def __init__(self, chunks):
            self._chunks = chunks

        def iter_chunked(self, size):
            return _ChunkIter(self._chunks)

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"Content-Length": content_length} if content_length else {}
    mock_resp.content = _Content(chunks)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    return mock_aiohttp, mock_session


class TestDownloadVoice:
    @pytest.mark.asyncio
    async def test_download_voice(self, platform):
        """Downloads from URL and returns a temp file path (streamed)."""
        import os

        mock_aiohttp, _ = _make_aiohttp_mock([b"fake-", b"ogg-", b"data"])
        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            path = await platform.download_voice("https://cdn.discord.com/attachments/voice.ogg")

        assert path.endswith(".ogg")
        with open(path, "rb") as f:
            assert f.read() == b"fake-ogg-data"
        os.unlink(path)

    @pytest.mark.asyncio
    async def test_download_voice_rejects_non_discord_host(self, platform):
        """SSRF guard — any host outside the Discord allow-list is refused
        BEFORE a network request is made."""
        mock_aiohttp, mock_session = _make_aiohttp_mock([b""])
        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(ValueError, match="non-Discord"):
                await platform.download_voice("https://attacker.example.com/evil.ogg")
            mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_voice_rejects_non_https(self, platform):
        mock_aiohttp, mock_session = _make_aiohttp_mock([b""])
        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(ValueError, match="non-HTTPS"):
                await platform.download_voice("http://cdn.discord.com/voice.ogg")
            mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_voice_rejects_oversize_content_length(self, platform):
        """If Content-Length declares > 25 MB, we bail before reading any
        bytes. Prevents memory exhaustion from a hostile upload."""
        import os

        too_big = str(26 * 1024 * 1024)
        mock_aiohttp, _ = _make_aiohttp_mock([b"x"], content_length=too_big)

        tmp_before = set(os.listdir(tempfile.gettempdir()))
        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(ValueError, match="cap"):
                await platform.download_voice("https://cdn.discord.com/huge.ogg")
        tmp_after = set(os.listdir(tempfile.gettempdir()))
        # The NamedTemporaryFile was unlinked on failure
        leaked = [f for f in (tmp_after - tmp_before) if f.endswith(".ogg")]
        assert leaked == []

    @pytest.mark.asyncio
    async def test_download_voice_rejects_oversize_streamed(self, platform):
        """Server lies about Content-Length (or omits it) but the body is
        actually huge. The streaming loop's running-total guard catches it
        before the file fills the disk."""
        # One 1 MB chunk × 30 ⇒ 30 MB total, no Content-Length.
        chunks = [b"x" * (1024 * 1024) for _ in range(30)]
        mock_aiohttp, _ = _make_aiohttp_mock(chunks, content_length=None)

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(ValueError, match="cap"):
                await platform.download_voice("https://cdn.discord.com/bomb.ogg")


class TestValidateDiscordUrl:
    """Direct unit tests for the URL guard shared by every download path."""

    def test_accepts_discord_com(self):
        from messaging.discord import _validate_discord_url
        _validate_discord_url("https://discord.com/file.ogg")

    def test_accepts_discordapp_com(self):
        from messaging.discord import _validate_discord_url
        _validate_discord_url("https://cdn.discordapp.com/attachments/voice.ogg")

    def test_accepts_discordapp_net(self):
        """Discord's media CDN uses the .net TLD."""
        from messaging.discord import _validate_discord_url
        _validate_discord_url("https://media.discordapp.net/attachments/img.png")

    def test_accepts_subdomain(self):
        from messaging.discord import _validate_discord_url
        _validate_discord_url("https://cdn.ptb.discord.com/file.ogg")

    def test_rejects_http(self):
        from messaging.discord import _validate_discord_url
        with pytest.raises(ValueError, match="non-HTTPS"):
            _validate_discord_url("http://cdn.discordapp.com/voice.ogg")

    def test_rejects_lookalike(self):
        from messaging.discord import _validate_discord_url
        with pytest.raises(ValueError, match="non-Discord"):
            _validate_discord_url("https://discord.com.attacker.example/file.ogg")

    def test_rejects_non_discord_host(self):
        from messaging.discord import _validate_discord_url
        with pytest.raises(ValueError, match="non-Discord"):
            _validate_discord_url("https://evil.example/file.ogg")


# ═══════════════════════════════════════════════════════════════════════════
# create_channel
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateChannel:
    @pytest.mark.asyncio
    async def test_create_text_channel(self, platform_with_client):
        """Falls back to text channel when no Workspaces forum exists."""
        mock_guild = MagicMock()
        mock_guild.channels = []  # no forum channel
        new_channel = MagicMock(id=777)
        mock_guild.create_text_channel = AsyncMock(return_value=new_channel)

        platform_with_client._client.get_guild = MagicMock(return_value=mock_guild)

        result = await platform_with_client.create_channel("my-workspace")
        assert result == 777
        mock_guild.create_text_channel.assert_awaited_once_with(name="my-workspace")

    @pytest.mark.asyncio
    async def test_create_forum_thread(self, platform_with_client):
        """Creates a thread in the Workspaces forum channel if it exists."""
        import discord

        mock_thread = MagicMock(id=888)
        mock_initial_msg = MagicMock()

        mock_forum = MagicMock(spec=discord.ForumChannel)
        mock_forum.name = "workspaces"
        mock_forum.create_thread = AsyncMock(return_value=(mock_thread, mock_initial_msg))

        mock_guild = MagicMock()
        mock_guild.channels = [mock_forum]

        platform_with_client._client.get_guild = MagicMock(return_value=mock_guild)

        result = await platform_with_client.create_channel("my-workspace")
        assert result == 888

    @pytest.mark.asyncio
    async def test_create_channel_guild_not_found(self, platform_with_client):
        platform_with_client._client.get_guild = MagicMock(return_value=None)
        platform_with_client._client.fetch_guild = AsyncMock(side_effect=Exception("nope"))
        result = await platform_with_client.create_channel("fail")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# close_channel
# ═══════════════════════════════════════════════════════════════════════════


class TestCloseChannel:
    @pytest.mark.asyncio
    async def test_close_thread(self, platform_with_client):
        """Archives a thread."""
        mock_thread = AsyncMock()
        mock_thread.archived = False
        mock_thread.edit = AsyncMock()
        platform_with_client._client.get_channel = MagicMock(return_value=mock_thread)

        result = await platform_with_client.close_channel(888)
        assert result is True
        mock_thread.edit.assert_awaited_once_with(archived=True)

    @pytest.mark.asyncio
    async def test_close_channel_delete(self, platform_with_client):
        """Deletes a channel that is not a thread."""
        mock_ch = AsyncMock()
        # No 'archived' attribute -> not a thread
        del mock_ch.archived
        mock_ch.delete = AsyncMock()
        platform_with_client._client.get_channel = MagicMock(return_value=mock_ch)

        result = await platform_with_client.close_channel(777)
        assert result is True
        mock_ch.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_channel_not_found(self, platform_with_client):
        platform_with_client._client.get_channel = MagicMock(return_value=None)
        platform_with_client._client.fetch_channel = AsyncMock(side_effect=Exception("nope"))
        result = await platform_with_client.close_channel(999)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# send_to_channel
# ═══════════════════════════════════════════════════════════════════════════


class TestSendToChannel:
    @pytest.mark.asyncio
    async def test_send_to_channel(self, platform_with_client, mock_channel):
        platform_with_client._client.get_channel = MagicMock(return_value=mock_channel)
        result = await platform_with_client.send_to_channel(100, "hello")
        assert result is True
        mock_channel.send.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_send_to_channel_not_found(self, platform_with_client):
        platform_with_client._client.get_channel = MagicMock(return_value=None)
        platform_with_client._client.fetch_channel = AsyncMock(side_effect=Exception("nope"))
        result = await platform_with_client.send_to_channel(100, "fail")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_to_channel_error(self, platform_with_client):
        mock_ch = AsyncMock()
        mock_ch.send = AsyncMock(side_effect=Exception("permission denied"))
        platform_with_client._client.get_channel = MagicMock(return_value=mock_ch)
        result = await platform_with_client.send_to_channel(100, "fail")
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# set_bot
# ═══════════════════════════════════════════════════════════════════════════


class TestSetBot:
    def test_set_bot(self, platform):
        client = MagicMock()
        platform.set_bot(client)
        assert platform._client is client
