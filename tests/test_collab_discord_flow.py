"""Spec 007 Phase 8 — end-to-end Discord collaborative-workspace flows.

The adapter side (audit-log retry, channel pick, dataclass dispatch) is
covered by ``tests/test_collab_discord_lifecycle.py``. The handler side
(refusal matrix, /im-the-owner, shared-guild policy) is covered by
``tests/test_collab_im_the_owner.py`` and
``tests/test_collab_multiplatform.py``. This file fills the remaining
gap: the **integrated** Flow A (pre-announce → bind), Flow B (ad-hoc
setup), and bot-removed paths exercised through ``collab_bot_added`` /
``collab_bot_removed`` with platform=discord lifecycle events.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers
from messaging.base import ChatRef, LifecycleAdded, LifecycleRemoved


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


@pytest.fixture
def hq_platform(mock_platform):
    """A platform that looks like Telegram HQ — required by
    ``_handle_collab_announce``'s ``is_main_thread`` guard."""
    mock_platform.is_main_thread.side_effect = (
        lambda chat_id, thread_id: thread_id is None
    )
    return mock_platform


# ── Flow A: pre-announce → bind with audit-log inviter match ─────────


class TestFlowADiscord:
    @pytest.mark.asyncio
    async def test_pre_announce_with_platform_discord_creates_pending(
        self, handlers, store, hq_platform,
    ):
        """The orchestrator's [COLLAB_ANNOUNCE] now accepts ``platform=``
        and ``creator_id=`` so a Telegram HQ session can announce a
        Discord workspace pinned to a Discord user id."""
        await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="atlas-007" display="Atlas" '
            'platform="discord" creator_id="456789012345678901" '
            'purpose="Cross-platform atlas test" '
            'inherit="" inherit_memory="true"]',
            chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        pending = [
            w for w in store.list_all()
            if w.name == "atlas-007"
        ]
        assert len(pending) == 1
        ws = pending[0]
        assert ws.status == "pending"
        assert ws.platform == "discord"
        assert ws.expected_platform == "discord"
        assert ws.expected_creator_id == 456789012345678901
        assert ws.chat_id == "0"

    @pytest.mark.asyncio
    async def test_pre_announce_telegram_keeps_back_compat(
        self, handlers, store, hq_platform,
    ):
        """An announce without ``platform=`` MUST default to Telegram and
        leave ``expected_platform=None`` (pre-007 behaviour)."""
        await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="legacy" display="Legacy" '
            'purpose="back-compat" inherit="" inherit_memory="true"]',
            chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        ws = [w for w in store.list_all() if w.name == "legacy"][0]
        assert ws.platform == "telegram"
        assert ws.expected_platform is None

    @pytest.mark.asyncio
    async def test_pre_announce_unknown_platform_rejected(
        self, handlers, store, hq_platform,
    ):
        out = await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="bad" display="Bad" platform="mastodon" '
            'purpose="nope" inherit="" inherit_memory="true"]',
            chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        assert "unknown platform" in out
        # No pending workspace persisted.
        assert [w for w in store.list_all() if w.name == "bad"] == []

    @pytest.mark.asyncio
    async def test_flow_a_audit_log_match_binds_workspace(
        self, handlers, store, hq_platform, mock_platform,
    ):
        """Pre-announce a Discord workspace, then fire LifecycleAdded
        with matching ``added_by_id`` — workspace binds, invite is
        generated, HQ notified."""
        # Step 1 — pre-announce on Telegram HQ.
        await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="atlas-007" display="Atlas Test" '
            'platform="discord" creator_id="456789012345678901" '
            'purpose="Cross-platform atlas test" '
            'inherit="" inherit_memory="true"]',
            chat_id=-100999, platform=hq_platform, thread_id=None,
        )

        # Step 2 — simulate the Discord adapter's on_guild_join firing
        # after a successful audit-log lookup.
        mock_platform.send_message = AsyncMock()
        mock_platform.get_invite_link = AsyncMock(
            return_value="https://discord.gg/test-invite",
        )
        event = LifecycleAdded(
            chat_ref=ChatRef("discord", "111:222"),
            chat_title="Atlas Guild",
            added_by_id=456789012345678901,
            added_by_name="alice",
        )
        await handlers["collab_bot_added"](mock_platform, event)

        # Workspace is bound to the channel.
        bound = next(w for w in store.list_all() if w.name == "atlas-007")
        assert bound.status == "active"
        assert bound.platform == "discord"
        assert bound.chat_id == "111:222"
        assert bound.invite_link == "https://discord.gg/test-invite"

        # In-channel welcome posted.
        channel_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == 222
        ]
        assert channel_sends, "Expected welcome in the bound channel"
        assert "Atlas Test" in channel_sends[0].kwargs["text"]

        # HQ notification fired.
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends, "Expected HQ notification"
        hq_text = hq_sends[0].kwargs["text"]
        assert "Atlas Test" in hq_text
        assert "Cross-platform atlas test" in hq_text
        # The invite link surfaces in the HQ message.
        assert "discord.gg/test-invite" in hq_text

    @pytest.mark.asyncio
    async def test_flow_a_cross_platform_does_not_bind(
        self, handlers, store, hq_platform, mock_platform,
    ):
        """A Telegram add for a user id that matches a Discord-pending
        workspace's expected_creator_id must NOT bind it (FR-013)."""
        await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="atlas-007" display="Atlas Test" '
            'platform="discord" creator_id="123" '
            'purpose="x" inherit="" inherit_memory="true"]',
            chat_id=-100999, platform=hq_platform, thread_id=None,
        )

        # A Telegram add by user id 123 should NOT bind the Discord
        # workspace — the platform filter in list_pending_for_creator
        # excludes it.
        mock_platform.send_message = AsyncMock()
        with patch("handlers.invoke_ai", new=AsyncMock(return_value="hi")):
            event = LifecycleAdded(
                chat_ref=ChatRef("telegram", "-100777"),
                chat_title="Stranger TG Group",
                added_by_id=123,
            )
            await handlers["collab_bot_added"](mock_platform, event)

        atlas = next(w for w in store.list_all() if w.name == "atlas-007")
        assert atlas.status == "pending"
        assert atlas.chat_id == "0"


# ── Flow B: ad-hoc add on Discord → setup workspace platform=discord ─


class TestFlowBDiscord:
    @pytest.mark.asyncio
    async def test_ad_hoc_add_creates_setup_workspace_with_discord_platform(
        self, handlers, store, mock_platform,
    ):
        """An authorized user adds Robyx to a Discord guild without a
        pre-announce. The handler must create a provisional workspace
        with ``platform="discord"`` (not the default ``"telegram"``)."""
        mock_platform.send_message = AsyncMock()
        mock_platform.get_invite_link = AsyncMock(return_value=None)
        with patch("handlers.invoke_ai", new=AsyncMock(return_value="Hi.")):
            event = LifecycleAdded(
                chat_ref=ChatRef("discord", "888:999"),
                chat_title="Ad-hoc Discord Guild",
                added_by_id=12345,  # OWNER_ID from conftest
            )
            await handlers["collab_bot_added"](mock_platform, event)

        # Provisional workspace exists with the Discord platform.
        active = [w for w in store.list_all() if w.platform == "discord"]
        assert len(active) == 1
        ws = active[0]
        assert ws.platform == "discord"
        assert ws.chat_id == "888:999"
        assert ws.status == "setup"


# ── on_guild_remove → close every workspace in the guild ─────────────


class TestDiscordGuildRemove:
    @pytest.mark.asyncio
    async def test_guild_remove_sentinel_closes_all_workspaces_in_guild(
        self, handlers, store, mock_platform,
    ):
        """The Discord adapter emits LifecycleRemoved with
        ``chat_id="<guild>:0"`` as a sentinel meaning "the bot was kicked
        from the whole guild". The handler MUST iterate
        ``find_active_in_guild`` and close every workspace whose
        ``chat_id`` lives in that guild."""
        # Seed two workspaces in the same guild and one in a different guild.
        a = CollabWorkspace(
            id="a", name="proj-a", display_name="A", agent_name="a",
            chat_id="111:222", platform="discord", status="active",
        )
        b = CollabWorkspace(
            id="b", name="proj-b", display_name="B", agent_name="b",
            chat_id="111:333", platform="discord", status="active",
        )
        c = CollabWorkspace(
            id="c", name="proj-c", display_name="C", agent_name="c",
            chat_id="999:444", platform="discord", status="active",
        )
        for ws in (a, b, c):
            store.add(ws)

        mock_platform.send_message = AsyncMock()
        event = LifecycleRemoved(
            chat_ref=ChatRef("discord", "111:0"),  # guild-wide sentinel
            chat_title="Gone Guild",
        )
        await handlers["collab_bot_removed"](mock_platform, event)

        # Both workspaces in guild 111 are closed; the unrelated guild 999
        # workspace is untouched.
        assert store.get("a").status == "closed"
        assert store.get("b").status == "closed"
        assert store.get("c").status == "active"

        # HQ received one notification per closed workspace.
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert len(hq_sends) == 2

    @pytest.mark.asyncio
    async def test_guild_remove_sentinel_no_workspaces_is_noop(
        self, handlers, store, mock_platform,
    ):
        """When the guild had no workspaces, the handler must not raise
        and must not send any HQ notification."""
        mock_platform.send_message = AsyncMock()
        event = LifecycleRemoved(
            chat_ref=ChatRef("discord", "111:0"),
            chat_title="Empty Guild",
        )
        await handlers["collab_bot_removed"](mock_platform, event)
        # No HQ messages — no workspaces to notify about.
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends == []

    @pytest.mark.asyncio
    async def test_guild_remove_specific_channel_uses_normal_lookup(
        self, handlers, store, mock_platform,
    ):
        """A LifecycleRemoved with a non-sentinel channel id (no ``:0``)
        falls through to the standard ``get_by_chat_id`` path — only
        the matching workspace is closed."""
        a = CollabWorkspace(
            id="a", name="proj-a", display_name="A", agent_name="a",
            chat_id="111:222", platform="discord", status="active",
        )
        b = CollabWorkspace(
            id="b", name="proj-b", display_name="B", agent_name="b",
            chat_id="111:333", platform="discord", status="active",
        )
        store.add(a)
        store.add(b)

        mock_platform.send_message = AsyncMock()
        event = LifecycleRemoved(
            chat_ref=ChatRef("discord", "111:222"),
            chat_title="Channel A",
        )
        await handlers["collab_bot_removed"](mock_platform, event)

        assert store.get("a").status == "closed"
        assert store.get("b").status == "active"
