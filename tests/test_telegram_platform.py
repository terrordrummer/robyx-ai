"""Tests for bot/messaging/telegram.py — Telegram platform adapter.

This file intentionally keeps the surface small — the rest of the Telegram
adapter is exercised indirectly by the handlers and bot tests. Here we
cover only the pieces that are Telegram-specific enough to warrant direct
testing.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import httpx

from messaging.telegram import TelegramPlatform


@pytest.fixture
def telegram_platform():
    plat = TelegramPlatform(
        bot_token="test-token",
        chat_id=-100999,
        owner_id=42,
    )
    bot = MagicMock()
    bot.edit_general_forum_topic = AsyncMock()
    plat.set_bot(bot)
    return plat


class TestIsMainThread:
    def test_none_is_main(self, telegram_platform):
        assert telegram_platform.is_main_thread(-100999, None) is True

    def test_any_thread_is_not_main(self, telegram_platform):
        assert telegram_platform.is_main_thread(-100999, 42) is False


class TestRenameMainChannel:
    @pytest.mark.asyncio
    async def test_calls_edit_general_forum_topic(self, telegram_platform):
        ok = await telegram_platform.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )
        assert ok is True
        telegram_platform._bot.edit_general_forum_topic.assert_awaited_once_with(
            chat_id=-100999,
            name="Command Bridge",
        )

    @pytest.mark.asyncio
    async def test_returns_false_on_api_exception(self, telegram_platform):
        telegram_platform._bot.edit_general_forum_topic = AsyncMock(
            side_effect=Exception("missing can_manage_topics"),
        )
        ok = await telegram_platform.rename_main_channel(
            display_name="Command Bridge", slug="command-bridge",
        )
        assert ok is False


class TestControlRoomId:
    """The Telegram General topic of a forum supergroup is addressed with
    ``message_thread_id=0``. The earlier hard-coded ``1`` was rejected by
    recent Bot API versions and made every scheduler/boot/update message
    silently disappear."""

    def test_returns_zero_not_one(self, telegram_platform):
        assert telegram_platform.control_room_id == 0


class TestSendMessageRawHttpx:
    """``send_message`` bypasses python-telegram-bot and POSTs directly to
    the Bot API. This makes the failure mode predictable on macOS sleep/wake
    where PTB has been observed to hang for minutes."""

    @pytest.mark.asyncio
    async def test_posts_to_bot_api_with_thread_and_parse_mode(
        self, telegram_platform, monkeypatch
    ):
        captured: dict = {}

        class _FakeResponse:
            def json(self):
                return {"ok": True, "result": {"message_id": 99}}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kwargs):
                captured["url"] = url
                captured["data"] = data
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await telegram_platform.send_message(
            chat_id=-100999,
            text="hello",
            thread_id=42,
            parse_mode="markdown",
        )

        assert result == {"message_id": 99}
        assert captured["url"].endswith("/sendMessage")
        assert captured["data"]["chat_id"] == -100999
        assert captured["data"]["text"] == "hello"
        assert captured["data"]["message_thread_id"] == 42
        assert captured["data"]["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_omits_thread_when_none(self, telegram_platform, monkeypatch):
        captured: dict = {}

        class _FakeResponse:
            def json(self):
                return {"ok": True, "result": {}}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kwargs):
                captured["data"] = data
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())

        await telegram_platform.send_message(
            chat_id=-100999, text="x", thread_id=None,
        )
        assert "message_thread_id" not in captured["data"]

    @pytest.mark.asyncio
    async def test_raises_on_api_failure(self, telegram_platform, monkeypatch):
        class _FakeResponse:
            def json(self):
                return {"ok": False, "description": "boom"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kwargs):
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())

        with pytest.raises(RuntimeError, match="boom"):
            await telegram_platform.send_message(
                chat_id=-100999, text="x", thread_id=0,
            )


# ─────────────────────────────────────────────────────────────────────
# Persistent httpx client (v0.20.16): every Telegram API call must
# reuse a single AsyncClient so connection pooling actually works.
# Pre-0.20.16 each ``send_typing`` / ``send_message`` instantiated a
# fresh client → cold TLS handshake on every call → 200-500ms latency
# on the first send after a quiet period (the root cause of the
# "typing doesn't appear immediately in Headquarters" bug).
# ─────────────────────────────────────────────────────────────────────


class TestPersistentHttpxClient:
    @pytest.mark.asyncio
    async def test_get_client_returns_same_instance_across_calls(self, telegram_platform):
        c1 = telegram_platform._get_client()
        c2 = telegram_platform._get_client()
        c3 = telegram_platform._get_client()
        assert c1 is c2 is c3, "platform must reuse a single AsyncClient"

    @pytest.mark.asyncio
    async def test_send_typing_does_not_create_a_new_client_per_call(
        self, telegram_platform, monkeypatch,
    ):
        """Track AsyncClient construction count: must be 1 even after N
        send_typing calls. This is the regression guard for the
        cold-handshake-per-call latency bug."""
        instances: list = []

        class _FakeResponse:
            def json(self):
                return {"ok": True}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                instances.append(self)

            async def post(self, url, data=None, **kwargs):
                return _FakeResponse()

            async def aclose(self):
                pass

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        # Reset any previously-cached client (the fixture may have
        # constructed one via the real httpx).
        telegram_platform._client = None

        for _ in range(5):
            await telegram_platform.send_typing(chat_id=-100999, thread_id=None)

        assert len(instances) == 1, (
            "expected exactly 1 AsyncClient across 5 send_typing calls, "
            "got %d — connection pooling is broken" % len(instances)
        )

    @pytest.mark.asyncio
    async def test_send_typing_omits_thread_id_for_general_topic(
        self, telegram_platform, monkeypatch,
    ):
        """Headquarters = General topic = ``thread_id=None``. Typing
        must be sent WITHOUT ``message_thread_id`` so Telegram displays
        it in General. Passing 0 (an earlier mistake) was rejected by
        recent Bot API versions and silently produced no indicator."""
        captured: dict = {}

        class _FakeResponse:
            def json(self):
                return {"ok": True}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def post(self, url, data=None, **kwargs):
                captured["url"] = url
                captured["data"] = data
                return _FakeResponse()

            async def aclose(self):
                pass

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        telegram_platform._client = None

        await telegram_platform.send_typing(chat_id=-100999, thread_id=None)

        assert captured["url"].endswith("/sendChatAction")
        assert captured["data"]["chat_id"] == -100999
        assert captured["data"]["action"] == "typing"
        assert "message_thread_id" not in captured["data"]

    @pytest.mark.asyncio
    async def test_send_typing_includes_thread_id_for_forum_topic(
        self, telegram_platform, monkeypatch,
    ):
        """Forum topics (thread_id != None) must include
        ``message_thread_id`` so the indicator lands in the right
        topic instead of leaking into General."""
        captured: dict = {}

        class _FakeResponse:
            def json(self):
                return {"ok": True}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def post(self, url, data=None, **kwargs):
                captured["data"] = data
                return _FakeResponse()

            async def aclose(self):
                pass

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        telegram_platform._client = None

        await telegram_platform.send_typing(chat_id=-100999, thread_id=42)

        assert captured["data"]["message_thread_id"] == 42

    @pytest.mark.asyncio
    async def test_aclose_resets_client(self, telegram_platform, monkeypatch):
        """``aclose`` must close the cached client and clear the cache
        so the next call lazy-creates a fresh one — important for
        clean shutdown and for tests that swap mocks."""
        closed_calls = {"n": 0}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def aclose(self):
                closed_calls["n"] += 1

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        telegram_platform._client = None
        telegram_platform._get_client()  # creates one
        await telegram_platform.aclose()
        assert closed_calls["n"] == 1
        assert telegram_platform._client is None


# ─────────────────────────────────────────────────────────────────────
# download_voice size-cap guard (Pass 2 P2-82). Telegram voice messages
# are capped by the Bot API at 50 MB, but we further bound the adapter's
# acceptance window to 25 MB so repeated-long-voice DoS cannot fill
# operator disks. Mirrors Pass 2 P2-11 on discord.py. The cap MUST hold
# both before download (via ``getFile`` file_size) and after download
# (defense-in-depth against a server lying between getFile and the
# download endpoint).
# ─────────────────────────────────────────────────────────────────────


class TestDownloadVoiceSizeCap:
    @pytest.mark.asyncio
    async def test_oversize_file_rejected_before_download(
        self, telegram_platform, tmp_path,
    ):
        from messaging import telegram as tg_mod

        voice_file = MagicMock()
        voice_file.file_size = tg_mod._MAX_TELEGRAM_VOICE_BYTES + 1
        voice_file.download_to_drive = AsyncMock()
        telegram_platform._bot.get_file = AsyncMock(return_value=voice_file)

        with pytest.raises(ValueError, match="exceeds"):
            await telegram_platform.download_voice("file-id-huge")
        voice_file.download_to_drive.assert_not_awaited(), (
            "declared-oversize must refuse up-front, no bytes on disk"
        )

    @pytest.mark.asyncio
    async def test_at_cap_file_is_accepted(
        self, telegram_platform, tmp_path,
    ):
        from messaging import telegram as tg_mod

        downloaded_paths: list[str] = []

        async def _fake_download(path):
            # Emulate a well-behaved server: write exactly 1 KB.
            downloaded_paths.append(path)
            with open(path, "wb") as f:
                f.write(b"x" * 1024)

        voice_file = MagicMock()
        voice_file.file_size = tg_mod._MAX_TELEGRAM_VOICE_BYTES  # exactly at cap
        voice_file.download_to_drive = AsyncMock(side_effect=_fake_download)
        telegram_platform._bot.get_file = AsyncMock(return_value=voice_file)

        out = await telegram_platform.download_voice("file-id-ok")
        assert out == downloaded_paths[0]
        # cleanup
        try:
            os_unlink = __import__("os").unlink
            os_unlink(out)
        except OSError:
            pass

    @pytest.mark.asyncio
    async def test_server_lies_about_size_post_download_cap_catches_it(
        self, telegram_platform, tmp_path, monkeypatch,
    ):
        """getFile claims small; the actual download writes more. The
        post-download size check must catch it and unlink the temp."""
        from messaging import telegram as tg_mod

        actual_path: dict[str, str] = {}

        async def _fake_download(path):
            actual_path["p"] = path
            with open(path, "wb") as f:
                f.write(b"y" * (tg_mod._MAX_TELEGRAM_VOICE_BYTES + 1))

        voice_file = MagicMock()
        voice_file.file_size = 100  # LIE — tiny declared size
        voice_file.download_to_drive = AsyncMock(side_effect=_fake_download)
        telegram_platform._bot.get_file = AsyncMock(return_value=voice_file)

        with pytest.raises(ValueError, match="exceeds"):
            await telegram_platform.download_voice("file-id-liar")
        # The post-download guard must unlink the oversized temp file.
        import os as _os
        assert not _os.path.exists(actual_path["p"]), (
            "oversized temp file must be unlinked on refusal"
        )

    @pytest.mark.asyncio
    async def test_missing_file_size_attribute_does_not_crash(
        self, telegram_platform, tmp_path,
    ):
        """Some telegram.File objects may lack ``file_size``. The code
        must treat that as 'unknown' and fall through to the
        post-download guard, not AttributeError."""

        async def _fake_download(path):
            with open(path, "wb") as f:
                f.write(b"z" * 2048)

        voice_file = MagicMock(spec=[])  # no file_size attribute
        voice_file.download_to_drive = AsyncMock(side_effect=_fake_download)
        telegram_platform._bot.get_file = AsyncMock(return_value=voice_file)

        out = await telegram_platform.download_voice("file-id-no-size")
        import os as _os
        try:
            _os.unlink(out)
        except OSError:
            pass

    @pytest.mark.asyncio
    async def test_download_error_cleans_up_tempfile(
        self, telegram_platform, tmp_path,
    ):
        """A download error (network drop, PTB exception) must still
        unlink the pre-created temp file. No on-disk residue for
        failed downloads."""
        created: dict[str, str] = {}

        async def _fake_download(path):
            created["p"] = path
            # File exists (tempfile created it), now simulate download fail.
            raise RuntimeError("transport error")

        voice_file = MagicMock()
        voice_file.file_size = 1024
        voice_file.download_to_drive = AsyncMock(side_effect=_fake_download)
        telegram_platform._bot.get_file = AsyncMock(return_value=voice_file)

        with pytest.raises(RuntimeError, match="transport error"):
            await telegram_platform.download_voice("file-id-transport-fail")
        import os as _os
        assert not _os.path.exists(created["p"]), (
            "tempfile must be unlinked when download raises"
        )
