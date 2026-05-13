"""Spec 007 Phase 6 — Discord ``leave_chat`` and ``get_invite_link``.

Covers:

- ``DISCORD_INVITE_TTL_DAYS`` / ``DISCORD_INVITE_MAX_USES`` defaults
  flow through to ``channel.create_invite(max_age=..., max_uses=...)``.
- Env-overridden values flow through.
- Invalid env values fall back to defaults with a WARN log.
- ``leave_chat`` parses ``"<guild>:<channel>"``, fetches the guild,
  calls ``guild.leave()``. ``discord.NotFound`` is treated as "already
  gone", not an error. Malformed chat_id refuses without raising.
- ``get_invite_link`` returns the invite URL on success, ``None`` on
  failure (refused permission, channel unreachable).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import discord
import pytest

from messaging.discord import DiscordPlatform


@pytest.fixture
def platform():
    return DiscordPlatform(
        bot_token="t", guild_id=111, owner_id=42, control_channel_id=444,
    )


@pytest.fixture
def platform_with_client(platform):
    client = MagicMock()
    client.user = MagicMock(id=9001)
    platform.set_bot(client)
    return platform


# ── Config env-var fallback ─────────────────────────────────────────────


class TestConfigEnvDefaults:
    """``_int_env_with_default`` is the contract for ``DISCORD_INVITE_*``
    knobs. Invalid values fall back to the documented defaults."""

    def test_unset_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("DISCORD_INVITE_TTL_DAYS", raising=False)
        monkeypatch.delenv("DISCORD_INVITE_MAX_USES", raising=False)
        import config
        importlib.reload(config)
        assert config.DISCORD_INVITE_TTL_DAYS == 7
        assert config.DISCORD_INVITE_MAX_USES == 10

    def test_valid_override_takes_effect(self, monkeypatch):
        monkeypatch.setenv("DISCORD_INVITE_TTL_DAYS", "30")
        monkeypatch.setenv("DISCORD_INVITE_MAX_USES", "5")
        import config
        importlib.reload(config)
        assert config.DISCORD_INVITE_TTL_DAYS == 30
        assert config.DISCORD_INVITE_MAX_USES == 5

    def test_zero_is_allowed_as_no_limit_sentinel(self, monkeypatch):
        # Discord's create_invite treats max_age=0 / max_uses=0 as "no
        # limit"; we accept it verbatim (operator's choice).
        monkeypatch.setenv("DISCORD_INVITE_TTL_DAYS", "0")
        monkeypatch.setenv("DISCORD_INVITE_MAX_USES", "0")
        import config
        importlib.reload(config)
        assert config.DISCORD_INVITE_TTL_DAYS == 0
        assert config.DISCORD_INVITE_MAX_USES == 0

    def test_non_integer_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_INVITE_TTL_DAYS", "garbage")
        monkeypatch.setenv("DISCORD_INVITE_MAX_USES", "")
        import config
        with caplog.at_level("WARNING"):
            importlib.reload(config)
        assert config.DISCORD_INVITE_TTL_DAYS == 7
        # Empty string ≡ unset → default without WARN.
        assert config.DISCORD_INVITE_MAX_USES == 10
        # The garbage value produced a WARN log.
        assert any(
            "DISCORD_INVITE_TTL_DAYS" in r.getMessage()
            and "garbage" in r.getMessage()
            for r in caplog.records
        )

    def test_negative_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_INVITE_TTL_DAYS", "-5")
        monkeypatch.setenv("DISCORD_INVITE_MAX_USES", "-1")
        import config
        with caplog.at_level("WARNING"):
            importlib.reload(config)
        assert config.DISCORD_INVITE_TTL_DAYS == 7
        assert config.DISCORD_INVITE_MAX_USES == 10
        assert any(
            "DISCORD_INVITE_TTL_DAYS" in r.getMessage()
            and "-5" in r.getMessage()
            for r in caplog.records
        )


# ── leave_chat ────────────────────────────────────────────────────────


class TestLeaveChat:
    async def test_leave_chat_calls_guild_leave(self, platform_with_client):
        guild = MagicMock()
        guild.leave = AsyncMock()
        platform_with_client._client.get_guild = MagicMock(return_value=guild)
        await platform_with_client.leave_chat("111:222")
        platform_with_client._client.get_guild.assert_called_once_with(111)
        guild.leave.assert_awaited_once()

    async def test_leave_chat_falls_back_to_fetch_when_get_returns_none(
        self, platform_with_client,
    ):
        guild = MagicMock()
        guild.leave = AsyncMock()
        platform_with_client._client.get_guild = MagicMock(return_value=None)
        platform_with_client._client.fetch_guild = AsyncMock(return_value=guild)
        await platform_with_client.leave_chat("111:222")
        platform_with_client._client.fetch_guild.assert_awaited_once_with(111)
        guild.leave.assert_awaited_once()

    async def test_leave_chat_not_found_is_no_op(self, platform_with_client):
        platform_with_client._client.get_guild = MagicMock(return_value=None)
        platform_with_client._client.fetch_guild = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "gone"),
        )
        # Must not raise.
        await platform_with_client.leave_chat("111:222")

    async def test_leave_chat_no_client_is_noop(self, platform):
        # Adapter set up but ``set_bot`` not called → no-op without raise.
        await platform.leave_chat("111:222")

    async def test_leave_chat_malformed_chat_id_refuses(
        self, platform_with_client, caplog,
    ):
        platform_with_client._client.get_guild = MagicMock()
        with caplog.at_level("WARNING"):
            await platform_with_client.leave_chat("not-a-discord-id")
        platform_with_client._client.get_guild.assert_not_called()
        assert any("malformed chat_id" in r.getMessage() for r in caplog.records)

    async def test_leave_chat_handles_sentinel_zero_channel(
        self, platform_with_client,
    ):
        """The on_guild_remove emit uses ``"<guild>:0"`` as a sentinel —
        ``leave_chat`` accepts it (only the guild half is used)."""
        guild = MagicMock()
        guild.leave = AsyncMock()
        platform_with_client._client.get_guild = MagicMock(return_value=guild)
        await platform_with_client.leave_chat("111:0")
        guild.leave.assert_awaited_once()


# ── get_invite_link ──────────────────────────────────────────────────


class TestGetInviteLink:
    async def test_uses_default_ttl_and_max_uses(
        self, platform_with_client, monkeypatch,
    ):
        # Patch the config attributes the adapter reads lazily.
        import config
        monkeypatch.setattr(config, "DISCORD_INVITE_TTL_DAYS", 7, raising=False)
        monkeypatch.setattr(config, "DISCORD_INVITE_MAX_USES", 10, raising=False)
        channel = MagicMock()
        invite = MagicMock()
        invite.__str__ = lambda self: "https://discord.gg/test123"
        channel.create_invite = AsyncMock(return_value=invite)
        platform_with_client._client.get_channel = MagicMock(return_value=channel)
        link = await platform_with_client.get_invite_link("111:222")
        assert link == "https://discord.gg/test123"
        channel.create_invite.assert_awaited_once_with(
            max_age=7 * 86400,
            max_uses=10,
            reason="Robyx collaborative workspace invite",
        )

    async def test_env_override_propagates(
        self, platform_with_client, monkeypatch,
    ):
        import config
        monkeypatch.setattr(config, "DISCORD_INVITE_TTL_DAYS", 30, raising=False)
        monkeypatch.setattr(config, "DISCORD_INVITE_MAX_USES", 5, raising=False)
        channel = MagicMock()
        channel.create_invite = AsyncMock(return_value=MagicMock())
        platform_with_client._client.get_channel = MagicMock(return_value=channel)
        await platform_with_client.get_invite_link("111:222")
        channel.create_invite.assert_awaited_once_with(
            max_age=30 * 86400,
            max_uses=5,
            reason="Robyx collaborative workspace invite",
        )

    async def test_zero_max_age_passed_as_no_limit(
        self, platform_with_client, monkeypatch,
    ):
        import config
        monkeypatch.setattr(config, "DISCORD_INVITE_TTL_DAYS", 0, raising=False)
        monkeypatch.setattr(config, "DISCORD_INVITE_MAX_USES", 0, raising=False)
        channel = MagicMock()
        channel.create_invite = AsyncMock(return_value=MagicMock())
        platform_with_client._client.get_channel = MagicMock(return_value=channel)
        await platform_with_client.get_invite_link("111:222")
        # max_age=0 propagates verbatim (Discord sentinel for "no limit").
        channel.create_invite.assert_awaited_once_with(
            max_age=0, max_uses=0,
            reason="Robyx collaborative workspace invite",
        )

    async def test_create_invite_failure_returns_none(self, platform_with_client):
        channel = MagicMock()
        channel.create_invite = AsyncMock(side_effect=discord.Forbidden(
            MagicMock(), "no perm",
        ))
        platform_with_client._client.get_channel = MagicMock(return_value=channel)
        link = await platform_with_client.get_invite_link("111:222")
        assert link is None

    async def test_channel_unreachable_returns_none(self, platform_with_client):
        platform_with_client._client.get_channel = MagicMock(return_value=None)
        platform_with_client._client.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "gone"),
        )
        link = await platform_with_client.get_invite_link("111:222")
        assert link is None

    async def test_malformed_chat_id_returns_none(self, platform_with_client):
        platform_with_client._client.get_channel = MagicMock()
        link = await platform_with_client.get_invite_link("garbage")
        assert link is None
        platform_with_client._client.get_channel.assert_not_called()
