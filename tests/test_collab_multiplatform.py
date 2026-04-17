"""Tests for the FR-013 'not yet supported on Discord/Slack' stubs."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from i18n import STRINGS


class TestDiscordLeaveChatRaises:
    @pytest.mark.asyncio
    async def test_leave_chat_not_implemented(self):
        from messaging.discord import DiscordPlatform
        plat = DiscordPlatform(
            bot_token="x", guild_id=None, owner_id=1, control_channel_id=None,
        )
        with pytest.raises(NotImplementedError):
            await plat.leave_chat(123)


class TestSlackLeaveChatRaises:
    @pytest.mark.asyncio
    async def test_leave_chat_not_implemented(self):
        from messaging.slack import SlackPlatform
        plat = SlackPlatform(
            bot_token="xoxb-test",
            channel_id="C0",
            owner_id="U1",
        )
        with pytest.raises(NotImplementedError):
            await plat.leave_chat("C0123")


class TestUnsupportedPlatformStrings:
    def test_discord_string_mentions_discord(self):
        assert "Discord" in STRINGS["collab_unsupported_platform_discord"]

    def test_slack_string_mentions_slack(self):
        assert "Slack" in STRINGS["collab_unsupported_platform_slack"]
