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

            async def post(self, url, data=None):
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

            async def post(self, url, data=None):
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

            async def post(self, url, data=None):
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())

        with pytest.raises(RuntimeError, match="boom"):
            await telegram_platform.send_message(
                chat_id=-100999, text="x", thread_id=0,
            )
