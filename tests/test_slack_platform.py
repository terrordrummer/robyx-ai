"""Tests for bot.messaging.slack — SlackPlatform adapter."""

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from messaging.slack import SlackPlatform


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def slack_platform():
    """A SlackPlatform with a mocked async client."""
    plat = SlackPlatform(
        bot_token="xoxb-test-token",
        channel_id="C01CONTROL",
        owner_id="U01OWNER",
    )
    client = AsyncMock()
    plat.set_bot(client)
    return plat


@pytest.fixture
def mock_client(slack_platform):
    """Return the mocked client from the platform."""
    return slack_platform._client


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_max_message_length(self, slack_platform):
        assert slack_platform.max_message_length == 4000

    def test_control_room_id_is_channel_id(self, slack_platform):
        assert slack_platform.control_room_id == "C01CONTROL"

    def test_control_room_id_consistent(self, slack_platform):
        """Same channel ID should always round-trip verbatim."""
        assert slack_platform.control_room_id == slack_platform.control_room_id

    def test_control_room_channel(self, slack_platform):
        assert slack_platform.control_room_channel == "C01CONTROL"


# ---------------------------------------------------------------------------
# is_owner
# ---------------------------------------------------------------------------

class TestIsOwner:
    def test_owner_matches_string(self, slack_platform):
        assert slack_platform.is_owner("U01OWNER") is True

    def test_non_owner_rejected(self, slack_platform):
        assert slack_platform.is_owner("U99OTHER") is False

    def test_owner_matches_int_comparison(self, slack_platform):
        """is_owner compares string representations, so int inputs work."""
        plat = SlackPlatform("xoxb-t", "C01", owner_id="12345")
        assert plat.is_owner(12345) is True

    def test_owner_mismatch_int(self, slack_platform):
        plat = SlackPlatform("xoxb-t", "C01", owner_id="12345")
        assert plat.is_owner(99999) is False


# ---------------------------------------------------------------------------
# is_main_thread
# ---------------------------------------------------------------------------

class TestSendPhoto:
    @pytest.mark.asyncio
    async def test_send_photo_calls_files_upload_v2(self, slack_platform, tmp_path):
        from PIL import Image
        img_path = tmp_path / "ok.png"
        Image.new("RGB", (32, 32), color=(255, 255, 0)).save(img_path, "PNG")

        slack_platform._client.files_upload_v2 = AsyncMock(return_value={"ok": True})
        await slack_platform.send_photo(
            chat_id="C01CONTROL",
            path=str(img_path),
            caption="slack caption",
            thread_id="1700000000.0001",
        )

        slack_platform._client.files_upload_v2.assert_awaited_once()
        kwargs = slack_platform._client.files_upload_v2.call_args[1]
        assert kwargs["channel"] == "C01CONTROL"
        assert kwargs["file"] == str(img_path)
        assert kwargs["initial_comment"] == "slack caption"
        assert kwargs["thread_ts"] == "1700000000.0001"

    @pytest.mark.asyncio
    async def test_send_photo_missing_file_returns_none(self, slack_platform, tmp_path):
        slack_platform._client.files_upload_v2 = AsyncMock()
        result = await slack_platform.send_photo(
            chat_id="C01CONTROL",
            path=str(tmp_path / "nope.png"),
        )
        assert result is None
        slack_platform._client.files_upload_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_photo_upload_exception_returns_none(self, slack_platform, tmp_path):
        from PIL import Image
        img_path = tmp_path / "ok.png"
        Image.new("RGB", (32, 32)).save(img_path, "PNG")

        slack_platform._client.files_upload_v2 = AsyncMock(side_effect=Exception("api down"))
        result = await slack_platform.send_photo(
            chat_id="C01CONTROL",
            path=str(img_path),
        )
        assert result is None


class TestRenameMainChannel:
    @pytest.mark.asyncio
    async def test_renames_control_channel(self, slack_platform):
        slack_platform._client.conversations_info = AsyncMock(
            return_value={"channel": {"name": "old-name"}},
        )
        slack_platform._client.conversations_rename = AsyncMock(return_value={"ok": True})

        ok = await slack_platform.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )

        assert ok is True
        slack_platform._client.conversations_rename.assert_awaited_once()
        kwargs = slack_platform._client.conversations_rename.call_args[1]
        assert kwargs["channel"] == "C01CONTROL"
        assert kwargs["name"] == "command-bridge"

    @pytest.mark.asyncio
    async def test_idempotent_when_already_named(self, slack_platform):
        slack_platform._client.conversations_info = AsyncMock(
            return_value={"channel": {"name": "command-bridge"}},
        )
        slack_platform._client.conversations_rename = AsyncMock()

        ok = await slack_platform.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )

        assert ok is True
        slack_platform._client.conversations_rename.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_on_api_exception(self, slack_platform):
        slack_platform._client.conversations_info = AsyncMock(
            side_effect=Exception("scope missing"),
        )
        ok = await slack_platform.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )
        assert ok is False


class TestIsMainThread:
    def test_control_channel_top_level_is_main(self, slack_platform):
        # Top-level message in the control-room channel: main.
        assert slack_platform.is_main_thread("C01CONTROL", None) is True

    def test_control_channel_in_thread_is_not_main(self, slack_platform):
        # Threaded reply in the control-room channel: not main.
        assert slack_platform.is_main_thread("C01CONTROL", "1700000000.0001") is False

    def test_other_channel_is_not_main(self, slack_platform):
        assert slack_platform.is_main_thread("C02OTHER", None) is False


# ---------------------------------------------------------------------------
# set_bot
# ---------------------------------------------------------------------------

class TestSetBot:
    def test_set_bot_stores_client(self):
        plat = SlackPlatform("xoxb-t", "C01", "U01")
        client = AsyncMock()
        plat.set_bot(client)
        assert plat._client is client


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_basic(self, slack_platform, mock_client):
        mock_client.chat_postMessage.return_value = {"channel": "C01", "ts": "1234.5678"}

        result = await slack_platform.send_message(chat_id="C01", text="hello")

        mock_client.chat_postMessage.assert_awaited_once_with(channel="C01", text="hello")
        assert result == {"channel": "C01", "ts": "1234.5678"}

    @pytest.mark.asyncio
    async def test_send_message_with_thread(self, slack_platform, mock_client):
        mock_client.chat_postMessage.return_value = {"channel": "C01", "ts": "1234.5678"}

        await slack_platform.send_message(chat_id="C01", text="reply", thread_id="1111.0000")

        mock_client.chat_postMessage.assert_awaited_once_with(
            channel="C01", text="reply", thread_ts="1111.0000",
        )

    @pytest.mark.asyncio
    async def test_send_message_no_thread_when_none(self, slack_platform, mock_client):
        mock_client.chat_postMessage.return_value = {"channel": "C01", "ts": "1234.5678"}

        await slack_platform.send_message(chat_id="C01", text="hi", thread_id=None)

        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert "thread_ts" not in call_kwargs


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------

class TestReply:
    @pytest.mark.asyncio
    async def test_reply_posts_in_thread(self, slack_platform, mock_client):
        mock_client.chat_postMessage.return_value = {"channel": "C01", "ts": "9999.0000"}
        msg_ref = {"channel": "C01", "ts": "1111.2222"}

        result = await slack_platform.reply(msg_ref, "thanks")

        mock_client.chat_postMessage.assert_awaited_once_with(
            channel="C01", text="thanks", thread_ts="1111.2222",
        )
        assert result == {"channel": "C01", "ts": "9999.0000"}


# ---------------------------------------------------------------------------
# edit_message
# ---------------------------------------------------------------------------

class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edit_message(self, slack_platform, mock_client):
        msg_ref = {"channel": "C01", "ts": "1111.2222"}

        await slack_platform.edit_message(msg_ref, "updated text")

        mock_client.chat_update.assert_awaited_once_with(
            channel="C01", ts="1111.2222", text="updated text",
        )


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------

class TestSendTyping:
    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self, slack_platform):
        """Slack does not support typing indicators for bots -- should not raise."""
        await slack_platform.send_typing(chat_id="C01")

    @pytest.mark.asyncio
    async def test_send_typing_with_thread_is_noop(self, slack_platform):
        await slack_platform.send_typing(chat_id="C01", thread_id="1111.0000")


# ---------------------------------------------------------------------------
# download_voice
# ---------------------------------------------------------------------------

def _mock_slack_response(content: bytes = b"audio", is_redirect: bool = False, location: str = ""):
    """Build a mocked httpx response with the sync attributes the new
    validator path reads (``is_redirect``, ``headers``, ``url``)."""
    resp = MagicMock()
    resp.content = content
    resp.is_redirect = is_redirect
    resp.headers = {"location": location} if location else {}
    resp.url = "https://files.slack.com/original"
    resp.raise_for_status = MagicMock()
    return resp


class TestDownloadVoice:
    @pytest.mark.asyncio
    async def test_download_voice_saves_file(self, slack_platform):
        mock_response = _mock_slack_response(content=b"audio-data-here")

        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            path = await slack_platform.download_voice("https://files.slack.com/audio.ogg")

        assert path.endswith(".ogg")
        # Verify auth header was passed
        call_kwargs = client_instance.get.call_args
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer xoxb-test-token"

    @pytest.mark.asyncio
    async def test_download_voice_uses_url_as_file_id(self, slack_platform):
        """file_id for Slack is the url_private_download URL."""
        mock_response = _mock_slack_response(content=b"data")

        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            url = "https://files.slack.com/files-pri/T01-F01/download/voice.ogg"
            await slack_platform.download_voice(url)

        client_instance.get.assert_awaited_once()
        assert client_instance.get.call_args[0][0] == url

    @pytest.mark.asyncio
    async def test_download_voice_rejects_non_slack_host(self, slack_platform):
        """SSRF guard: a crafted file_id pointing at an attacker host must
        be refused BEFORE any HTTP request is made — otherwise the bot
        token leaks to the attacker's server."""
        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            with pytest.raises(ValueError, match="non-Slack host"):
                await slack_platform.download_voice("https://attacker.example.com/steal")

            MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_voice_rejects_non_https(self, slack_platform):
        """Bearer token must never travel over plaintext HTTP."""
        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            with pytest.raises(ValueError, match="non-HTTPS"):
                await slack_platform.download_voice("http://files.slack.com/voice.ogg")

            MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_voice_rejects_cross_host_redirect(self, slack_platform):
        """A 302 Location pointing outside the Slack allow-list is rejected
        before the bearer token is replayed to the redirect target."""
        redirect_resp = _mock_slack_response(is_redirect=True, location="https://attacker.example.com/pickup")

        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = redirect_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="non-Slack host"):
                await slack_platform.download_voice("https://files.slack.com/initial.ogg")

            # Exactly one request was made (the initial) — the redirect was
            # validated and rejected before any cross-host fetch.
            assert client_instance.get.await_count == 1

    @pytest.mark.asyncio
    async def test_download_voice_follows_in_allowlist_redirect(self, slack_platform):
        """A 302 to another Slack-hosted URL is followed — the token is
        safe because the target is still in the allow-list."""
        redirect_resp = _mock_slack_response(is_redirect=True, location="https://files.slack-edge.com/final.ogg")
        final_resp = _mock_slack_response(content=b"final-audio")

        with patch("messaging.slack.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.side_effect = [redirect_resp, final_resp]
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            path = await slack_platform.download_voice("https://files.slack.com/initial.ogg")

        assert path.endswith(".ogg")
        assert client_instance.get.await_count == 2


class TestValidateSlackFileUrl:
    """Direct tests of the URL guard used by download_voice."""

    def test_accepts_files_slack_com(self):
        from messaging.slack import _validate_slack_file_url
        _validate_slack_file_url("https://files.slack.com/audio.ogg")

    def test_accepts_files_slack_edge_com(self):
        from messaging.slack import _validate_slack_file_url
        _validate_slack_file_url("https://files.slack-edge.com/audio.ogg")

    def test_accepts_subdomain(self):
        from messaging.slack import _validate_slack_file_url
        _validate_slack_file_url("https://cdn.files.slack.com/audio.ogg")

    def test_rejects_http(self):
        from messaging.slack import _validate_slack_file_url
        with pytest.raises(ValueError, match="non-HTTPS"):
            _validate_slack_file_url("http://files.slack.com/audio.ogg")

    def test_rejects_lookalike_host(self):
        from messaging.slack import _validate_slack_file_url
        with pytest.raises(ValueError, match="non-Slack"):
            _validate_slack_file_url("https://files.slack.com.attacker.example/audio.ogg")

    def test_rejects_empty_host(self):
        from messaging.slack import _validate_slack_file_url
        with pytest.raises(ValueError):
            _validate_slack_file_url("https:///audio.ogg")


# ---------------------------------------------------------------------------
# create_channel
# ---------------------------------------------------------------------------

class TestCreateChannel:
    @pytest.mark.asyncio
    async def test_create_channel_success(self, slack_platform, mock_client):
        mock_client.conversations_create.return_value = {
            "channel": {"id": "C0NEW123"},
        }

        result = await slack_platform.create_channel("test-workspace")

        mock_client.conversations_create.assert_awaited_once_with(
            name="test-workspace", is_private=False,
        )
        assert result == "C0NEW123"

    @pytest.mark.asyncio
    async def test_create_channel_failure(self, slack_platform, mock_client):
        mock_client.conversations_create.side_effect = Exception("name_taken")

        result = await slack_platform.create_channel("existing-name")

        assert result is None


# ---------------------------------------------------------------------------
# close_channel
# ---------------------------------------------------------------------------

class TestCloseChannel:
    @pytest.mark.asyncio
    async def test_close_channel_success(self, slack_platform, mock_client):
        mock_client.conversations_archive.return_value = {"ok": True}

        result = await slack_platform.close_channel("C01234")

        mock_client.conversations_archive.assert_awaited_once_with(channel="C01234")
        assert result is True

    @pytest.mark.asyncio
    async def test_close_channel_failure(self, slack_platform, mock_client):
        mock_client.conversations_archive.side_effect = Exception("already_archived")

        result = await slack_platform.close_channel("C01234")

        assert result is False


# ---------------------------------------------------------------------------
# send_to_channel
# ---------------------------------------------------------------------------

class TestSendToChannel:
    @pytest.mark.asyncio
    async def test_send_to_channel_success(self, slack_platform, mock_client):
        mock_client.chat_postMessage.return_value = {"ok": True}

        result = await slack_platform.send_to_channel("C01234", "notification text")

        mock_client.chat_postMessage.assert_awaited_once_with(
            channel="C01234", text="notification text",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_send_to_channel_failure(self, slack_platform, mock_client):
        mock_client.chat_postMessage.side_effect = Exception("channel_not_found")

        result = await slack_platform.send_to_channel("C_INVALID", "text")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_to_channel_ignores_parse_mode(self, slack_platform, mock_client):
        """Slack uses mrkdwn by default -- parse_mode is accepted but not forwarded."""
        mock_client.chat_postMessage.return_value = {"ok": True}

        await slack_platform.send_to_channel("C01", "text", parse_mode="markdown")

        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert "parse_mode" not in call_kwargs
