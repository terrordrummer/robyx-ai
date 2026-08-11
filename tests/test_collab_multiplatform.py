"""Cross-platform collaborative-workspace tests for specs 003/007.

Spec 007: Discord's ``leave_chat`` is now implemented (see
``tests/test_collab_discord_invite.py``). Slack's remains unimplemented
pending spec 008. The shared-guild ``leave_chat`` policy in
``bot/handlers.py:collab_bot_added`` is exercised here as an end-to-end
test through the unauthorized-adder guard.
"""

from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers
from i18n import STRINGS
from messaging.base import ChatRef, LifecycleAdded


class TestSlackLeaveChatRaises:
    """Slack remains 'not yet supported' until spec 008."""

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
    """Spec 007 keeps the Discord notice STRING through Phase 6; Phase 7
    (i18n cleanup) removes ``collab_unsupported_platform_discord``. Slack
    remains documented until spec 008 closes it."""

    def test_slack_string_mentions_slack(self):
        assert "Slack" in STRINGS["collab_unsupported_platform_slack"]


# ── Spec 007 — shared-guild ``leave_chat`` policy ─────────────────────


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


class TestSharedGuildLeaveChatPolicy:
    """The ``leave_chat`` policy lives in the handler, not the adapter.

    Adapter ``DiscordPlatform.leave_chat`` is "dumb" — it always calls
    ``guild.leave()``. The handler MUST consult
    ``CollabStore.find_active_in_guild(guild_id)`` and refuse the leave
    if another active or setup workspace shares the guild — otherwise
    the unauthorized-adder refusal flow would orphan legitimate
    workspaces every time an outsider tried to add the bot to a shared
    guild (FR-016).
    """

    @pytest.mark.asyncio
    async def test_skips_leave_when_guild_has_peer_workspace(
        self, handlers, store, mock_platform,
    ):
        # Seed an active workspace in guild 111, channel 222.
        seed = CollabWorkspace(
            id="c-seed",
            name="seed",
            display_name="Seed",
            agent_name="seed",
            chat_id="111:222",
            platform="discord",
            status="active",
            created_by=10,
            roles={"10": "owner"},
        )
        store.add(seed)

        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        # Unauthorized adder triggers the refusal flow in a DIFFERENT
        # channel of the same guild.
        event = LifecycleAdded(
            chat_ref=ChatRef("discord", "111:333"),
            chat_title="Offending Channel",
            added_by_id=999_999,  # no role anywhere
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # Refusal message went to the offending channel.
        sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == 333
        ]
        assert sends, "Expected refusal in the offending channel"

        # leave_chat MUST NOT be called — peer workspace lives in guild 111.
        mock_platform.leave_chat.assert_not_called()

        # Peer workspace unchanged.
        peer = store.get("c-seed")
        assert peer.status == "active"
        assert peer.chat_id == "111:222"

    @pytest.mark.asyncio
    async def test_leaves_when_guild_has_no_peer_workspace(
        self, handlers, store, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        # Unauthorized adder in a guild where no workspace exists.
        event = LifecycleAdded(
            chat_ref=ChatRef("discord", "888:222"),
            chat_title="Stranger Guild",
            added_by_id=999_999,
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # leave_chat is called once with the offending chat_id.
        mock_platform.leave_chat.assert_awaited_once_with("888:222")

    @pytest.mark.asyncio
    async def test_telegram_path_unaffected_by_policy(
        self, handlers, store, mock_platform,
    ):
        """Telegram chats are 1:1 to workspaces — ``find_active_in_guild``
        is Discord-only, so the policy is inert for Telegram refusals."""
        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        event = LifecycleAdded(
            chat_ref=ChatRef("telegram", "-100777"),
            chat_title="Stranger Telegram Group",
            added_by_id=999_999,
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # leave_chat is called for the Telegram chat — no policy
        # short-circuit applies.
        mock_platform.leave_chat.assert_awaited_once_with("-100777")


class TestDiscordAuditLogAdvisory:
    """Spec 007 — when Discord's audit-log inviter lookup fails, the
    adapter emits ``LifecycleAdded(added_by_id=None)``. The handler MUST
    post the ``discord_audit_log_unavailable`` advisory in the channel
    and exit BEFORE the unauthorised-adder refusal flow — otherwise
    every audit-log failure would silently leave the guild and leave
    legitimate pending workspaces in limbo.
    """

    @pytest.mark.asyncio
    async def test_audit_log_failure_posts_advisory_and_exits(
        self, handlers, store, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        event = LifecycleAdded(
            chat_ref=ChatRef("discord", "111:222"),
            chat_title="Mystery Guild",
            added_by_id=None,  # audit-log lookup failed
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # Advisory posted in the offending channel.
        sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == 222
        ]
        assert sends, "Expected advisory in the channel"
        assert "View Audit Log" in sends[0].kwargs["text"]
        assert "/im-the-owner" in sends[0].kwargs["text"]

        # leave_chat MUST NOT be called — user may still claim manually.
        mock_platform.leave_chat.assert_not_called()

        # No workspace state was touched.
        assert store.list_all() == []

    @pytest.mark.asyncio
    async def test_telegram_event_with_none_adder_still_goes_to_unauth_flow(
        self, handlers, store, mock_platform,
    ):
        """The advisory shortcut is Discord-only — Telegram's
        ChatMember update reliably carries the inviter, so a None there
        is a genuine "unknown adder" case and the existing FR-011
        refusal flow still applies."""
        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        event = LifecycleAdded(
            chat_ref=ChatRef("telegram", "-100777"),
            chat_title="Telegram Mystery",
            added_by_id=None,
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # FR-011 refusal flow fires — leave_chat is invoked.
        mock_platform.leave_chat.assert_awaited_once_with("-100777")
