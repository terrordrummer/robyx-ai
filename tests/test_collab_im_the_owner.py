"""Spec 007 Phase 4 — ``/im-the-owner <workspace-name>`` manual claim.

Covers the deterministic refusal matrix from
``specs/007-discord-parity/contracts/im-the-owner.md`` plus the happy path
where a pending Discord workspace is bound to the channel the command
was issued in. All tests use the in-process ``CollabStore`` + a mocked
platform — no live discord.py connection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import pytest

from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers
from messaging.base import PlatformMessage


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


def _pending_discord_ws(
    name: str = "atlas-007",
    *,
    expected_creator_id: int | None = 7777,
    expected_platform: str | None = "discord",
    display_name: str = "Atlas Test",
) -> CollabWorkspace:
    return CollabWorkspace(
        id="collab-%s" % name,
        name=name,
        display_name=display_name,
        agent_name=name,
        chat_id="0",
        platform="discord",
        expected_platform=expected_platform,
        status="pending",
        created_by=expected_creator_id or 0,
        expected_creator_id=expected_creator_id,
        roles={
            str(expected_creator_id): "owner",
        } if expected_creator_id is not None else {},
    )


def _msg(text: str, *, user_id: int = 7777, guild_id: int = 111, channel_id: int = 222):
    """Build a Discord-shaped PlatformMessage where chat_id is the guild
    and thread_id is the channel — matching what ``_run_discord``
    constructs in its ``on_message`` dispatch."""
    return PlatformMessage(
        user_id=user_id,
        chat_id=guild_id,
        text=text,
        thread_id=channel_id,
        command="im-the-owner",
        args=text.split()[1:],
    )


# ── Success path ────────────────────────────────────────────────────────


async def test_success_path_binds_workspace(handlers, store, mock_platform):
    ws = _pending_discord_ws("atlas-007", expected_creator_id=7777)
    store.add(ws)
    msg = _msg("/im-the-owner atlas-007", user_id=7777, guild_id=111, channel_id=222)
    msg_ref = object()
    await handlers["_handle_im_the_owner"](mock_platform, msg, msg_ref)

    bound = store.get(ws.id)
    assert bound.status == "active"
    assert bound.platform == "discord"
    assert bound.chat_id == "111:222"
    # Success reply emitted.
    replies = [c for c in mock_platform.reply.call_args_list
               if "now active" in c.args[1] or "now active" in c.kwargs.get("text", "")]
    assert replies, "Expected success reply"


async def test_success_path_emits_hq_notification(handlers, store, mock_platform):
    ws = _pending_discord_ws("atlas-007", expected_creator_id=7777)
    store.add(ws)
    msg = _msg("/im-the-owner atlas-007", user_id=7777, guild_id=111, channel_id=222)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    # HQ notification fired via send_message — same path as the audit-log
    # success route.
    hq_sends = [
        c for c in mock_platform.send_message.call_args_list
        if c.kwargs.get("chat_id") == -100999
    ]
    assert hq_sends, "Expected HQ notification on successful claim"


# ── Usage / parse error ────────────────────────────────────────────────


async def test_bare_command_returns_usage(handlers, store, mock_platform):
    msg = _msg("/im-the-owner", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert replies, "Expected usage reply"
    assert "im-the-owner" in replies[0].lower()


async def test_whitespace_only_arg_returns_usage(handlers, store, mock_platform):
    msg = _msg("/im-the-owner    ", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert replies, "Expected usage reply for whitespace-only arg"


# ── Refusal: unknown workspace ─────────────────────────────────────────


async def test_unknown_workspace_refusal(handlers, store, mock_platform):
    msg = _msg("/im-the-owner nonexistent", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    # No state was touched.
    assert store.list_all() == []
    # Golden refusal STRING surfaces.
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert any("pending workspace named" in r for r in replies)


# ── Refusal: already bound ─────────────────────────────────────────────


async def test_already_bound_refusal(handlers, store, mock_platform):
    ws = CollabWorkspace(
        id="collab-bound",
        name="bound-ws",
        display_name="Bound",
        agent_name="bound-ws",
        chat_id="111:222",
        platform="discord",
        status="active",  # already active — not claimable
        created_by=7777,
        roles={"7777": "owner"},
    )
    store.add(ws)
    msg = _msg("/im-the-owner bound-ws", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    # State unchanged.
    reloaded = store.get(ws.id)
    assert reloaded.status == "active"
    assert reloaded.chat_id == "111:222"
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert any("already bound" in r for r in replies)


async def test_closed_workspace_refused_as_already_bound(
    handlers, store, mock_platform,
):
    ws = _pending_discord_ws("gone-ws", expected_creator_id=7777)
    ws.status = "closed"
    store.add(ws)
    msg = _msg("/im-the-owner gone-ws", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    reloaded = store.get(ws.id)
    assert reloaded.status == "closed"


# ── Refusal: platform mismatch ─────────────────────────────────────────


async def test_telegram_pending_refused(handlers, store, mock_platform):
    """A pending workspace targeted at Telegram cannot be claimed via
    /im-the-owner on Discord."""
    ws = CollabWorkspace(
        id="collab-tg",
        name="tg-pending",
        display_name="Telegram Pending",
        agent_name="tg-pending",
        chat_id="0",
        platform="telegram",
        expected_platform="telegram",
        status="pending",
        created_by=7777,
        expected_creator_id=7777,
        roles={"7777": "owner"},
    )
    store.add(ws)
    msg = _msg("/im-the-owner tg-pending", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    reloaded = store.get(ws.id)
    assert reloaded.status == "pending"
    assert reloaded.chat_id == "0"
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert any("pre-announced for" in r for r in replies)


# ── Refusal: creator mismatch ──────────────────────────────────────────


async def test_creator_mismatch_refused(handlers, store, mock_platform):
    ws = _pending_discord_ws("atlas-007", expected_creator_id=7777)
    store.add(ws)
    # Someone other than 7777 attempts the claim.
    msg = _msg("/im-the-owner atlas-007", user_id=42, guild_id=111, channel_id=222)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    reloaded = store.get(ws.id)
    assert reloaded.status == "pending"
    assert reloaded.chat_id == "0"
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert any("different user" in r for r in replies)


# ── Cross-platform user-id collision (FR-013) ──────────────────────────


async def test_expected_platform_mismatch_refused(handlers, store, mock_platform):
    """A pending workspace pre-announced for Slack must NOT bind via
    Discord /im-the-owner even if the creator id matches."""
    ws = _pending_discord_ws("atlas-007", expected_creator_id=7777)
    # Force expected_platform to slack — the handler must refuse.
    ws.expected_platform = "slack"
    store.add(ws)
    msg = _msg("/im-the-owner atlas-007", user_id=7777)
    await handlers["_handle_im_the_owner"](mock_platform, msg, object())
    reloaded = store.get(ws.id)
    assert reloaded.status == "pending"
    replies = [c.args[1] for c in mock_platform.reply.call_args_list]
    assert any("pre-announced for" in r for r in replies)
