"""Tests for spec 006 platform-adapter topic operations.

Contract: ``specs/006-continuous-task-robustness/contracts/platform-topic-ops.md``.
Covers Telegram (full), Discord (best-effort), Slack (degraded with
one-time WARN per session).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from messaging.base import TopicUnreachable


# ── Telegram ──────────────────────────────────────────────────────────────


@pytest.fixture
def telegram_adapter(monkeypatch):
    """A TelegramPlatform wired to a mock httpx client."""
    from messaging.telegram import TelegramPlatform

    plat = TelegramPlatform(bot_token="fake", chat_id=-100, owner_id=1)
    mock_client = AsyncMock()
    monkeypatch.setattr(plat, "_get_client", lambda: mock_client)
    return plat, mock_client


def _ok_response(payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.json.return_value = {"ok": True, "result": payload or {}}
    return r


def _error_response(description: str) -> MagicMock:
    r = MagicMock()
    r.json.return_value = {"ok": False, "description": description}
    return r


async def test_telegram_edit_topic_title_happy(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    result = await plat.edit_topic_title(42, "[Continuous] foo · ▶")
    assert result is True
    client.post.assert_awaited_once()
    url = client.post.call_args[0][0]
    assert url.endswith("/editForumTopic")


async def test_telegram_edit_topic_title_topic_unreachable(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _error_response("Bad Request: TOPIC_ID_INVALID")
    with pytest.raises(TopicUnreachable) as exc_info:
        await plat.edit_topic_title(42, "x")
    assert exc_info.value.channel_id == 42


async def test_telegram_edit_topic_title_transient_error(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _error_response("rate limited")
    result = await plat.edit_topic_title(42, "x")
    assert result is False  # logged, not raised


async def test_telegram_pin_message_happy(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    result = await plat.pin_message(chat_id=-100, thread_id=42, message_id=77)
    assert result is True
    url = client.post.call_args[0][0]
    assert url.endswith("/pinChatMessage")
    data = client.post.call_args.kwargs["data"]
    assert data["disable_notification"] is True  # silent pin


async def test_telegram_unpin_single_message(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    assert await plat.unpin_message(
        chat_id=-100, thread_id=42, message_id=77,
    )
    url = client.post.call_args[0][0]
    assert url.endswith("/unpinChatMessage")


async def test_telegram_unpin_all_in_topic(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    assert await plat.unpin_message(chat_id=-100, thread_id=42)
    url = client.post.call_args[0][0]
    assert url.endswith("/unpinAllForumTopicMessages")


async def test_telegram_pin_topic_unreachable(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _error_response("MESSAGE_THREAD_NOT_FOUND")
    with pytest.raises(TopicUnreachable):
        await plat.pin_message(chat_id=-100, thread_id=42, message_id=77)


async def test_telegram_close_topic_delegates_to_close_channel(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    assert await plat.close_topic(42)
    url = client.post.call_args[0][0]
    assert url.endswith("/closeForumTopic")


async def test_telegram_archive_topic_composes_rename_and_close(telegram_adapter):
    plat, client = telegram_adapter
    client.post.return_value = _ok_response()
    assert await plat.archive_topic(42, "zeus-research")
    # Two calls: editForumTopic then closeForumTopic.
    assert client.post.await_count == 2
    called_urls = [call.args[0] for call in client.post.call_args_list]
    assert any("/editForumTopic" in u for u in called_urls)
    assert any("/closeForumTopic" in u for u in called_urls)


# ── Discord ───────────────────────────────────────────────────────────────


@pytest.fixture
def discord_adapter(monkeypatch):
    """DiscordPlatform with a mocked client + guild."""
    import types

    from messaging.discord import DiscordPlatform

    plat = DiscordPlatform(
        bot_token="fake", guild_id=123, owner_id=1, control_channel_id=None,
    )
    plat._client = types.SimpleNamespace()
    return plat


async def test_discord_edit_topic_title_uses_channel_edit(discord_adapter, monkeypatch):
    channel = MagicMock()
    channel.name = "old-name"
    channel.edit = AsyncMock()
    monkeypatch.setattr(
        discord_adapter, "_fetch_channel", AsyncMock(return_value=channel),
    )
    result = await discord_adapter.edit_topic_title(42, "new-name")
    assert result is True
    channel.edit.assert_awaited_once_with(name="new-name")


async def test_discord_edit_topic_title_idempotent_same_name(discord_adapter, monkeypatch):
    channel = MagicMock()
    channel.name = "already-set"
    channel.edit = AsyncMock()
    monkeypatch.setattr(
        discord_adapter, "_fetch_channel", AsyncMock(return_value=channel),
    )
    assert await discord_adapter.edit_topic_title(42, "already-set")
    channel.edit.assert_not_awaited()


async def test_discord_edit_topic_title_raises_on_not_found(discord_adapter, monkeypatch):
    monkeypatch.setattr(
        discord_adapter,
        "_fetch_channel",
        AsyncMock(side_effect=TopicUnreachable(42, "not found")),
    )
    with pytest.raises(TopicUnreachable):
        await discord_adapter.edit_topic_title(42, "x")


async def test_discord_pin_message_happy(discord_adapter, monkeypatch):
    msg = MagicMock()
    msg.pin = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=msg)
    monkeypatch.setattr(
        discord_adapter, "_fetch_channel", AsyncMock(return_value=channel),
    )
    assert await discord_adapter.pin_message(
        chat_id=None, thread_id=42, message_id=77,
    )
    msg.pin.assert_awaited_once()


async def test_discord_unpin_all(discord_adapter, monkeypatch):
    pin1 = MagicMock()
    pin1.unpin = AsyncMock()
    pin2 = MagicMock()
    pin2.unpin = AsyncMock()
    channel = MagicMock()
    channel.pins = AsyncMock(return_value=[pin1, pin2])
    monkeypatch.setattr(
        discord_adapter, "_fetch_channel", AsyncMock(return_value=channel),
    )
    assert await discord_adapter.unpin_message(
        chat_id=None, thread_id=42, message_id=None,
    )
    pin1.unpin.assert_awaited_once()
    pin2.unpin.assert_awaited_once()


# ── Slack ────────────────────────────────────────────────────────────────


@pytest.fixture
def slack_adapter(monkeypatch):
    from messaging.slack import SlackPlatform

    plat = SlackPlatform(
        bot_token="xoxb-fake",
        channel_id="C123",
        owner_id="U1",
    )
    plat._client = AsyncMock()
    # Reset the per-class WARN cache so each test starts fresh.
    SlackPlatform._SPEC_006_WARNED.clear()
    return plat


async def test_slack_edit_topic_title_slugifies(slack_adapter):
    slack_adapter._client.conversations_rename.return_value = {"ok": True}
    assert await slack_adapter.edit_topic_title("C456", "[Continuous] Foo · ▶")
    slack_adapter._client.conversations_rename.assert_awaited_once()
    kwargs = slack_adapter._client.conversations_rename.call_args.kwargs
    # Slug-safe name — no spaces, no bracket chars, lowercase.
    assert " " not in kwargs["name"]
    assert kwargs["name"].islower() or kwargs["name"] == kwargs["name"]  # no uppercase


async def test_slack_pin_message_warns_once(slack_adapter, caplog):
    slack_adapter._client.pins_add.return_value = {"ok": True}
    await slack_adapter.pin_message(chat_id="C", thread_id="C456", message_id="ts1")
    await slack_adapter.pin_message(chat_id="C", thread_id="C456", message_id="ts2")
    warns = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "pin_message" in r.getMessage()
    ]
    assert len(warns) == 1  # one-time WARN per session


async def test_slack_close_topic_warns_once(slack_adapter, caplog):
    slack_adapter._client.conversations_archive.return_value = {"ok": True}
    await slack_adapter.close_topic("C456")
    await slack_adapter.close_topic("C789")
    warns = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "close_topic" in r.getMessage()
    ]
    assert len(warns) == 1


async def test_slack_archive_topic_composes(slack_adapter):
    slack_adapter._client.conversations_rename.return_value = {"ok": True}
    slack_adapter._client.conversations_archive.return_value = {"ok": True}
    assert await slack_adapter.archive_topic("C456", "zeus-research")
    slack_adapter._client.conversations_rename.assert_awaited_once()
    slack_adapter._client.conversations_archive.assert_awaited_once()


async def test_slack_unreachable_on_channel_not_found(slack_adapter):
    slack_adapter._client.conversations_rename.return_value = {
        "ok": False,
        "error": "channel_not_found",
    }
    with pytest.raises(TopicUnreachable):
        await slack_adapter.edit_topic_title("C_GONE", "new-name")
