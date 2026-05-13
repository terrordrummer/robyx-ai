"""Spec 007 Phase 2 — Discord adapter lifecycle plumbing.

Covers the adapter-side primitives only:

- ``DiscordPlatform.bot_user_id`` resolves from the underlying client.
- ``_resolve_inviter`` happy-path, Forbidden fail-fast, empty audit log,
  empty-then-success retry, sustained transient error.
- ``_pick_writable_channel`` system-channel preference, fallback to the
  first writable text channel, None when nothing is writable.
- ``register_lifecycle`` registers ``on_guild_join`` / ``on_guild_remove``
  on the client AND those callbacks emit
  :class:`bot.messaging.base.LifecycleAdded` /
  :class:`LifecycleRemoved` events through ``plat.on_added`` /
  ``plat.on_removed`` with the correct ``ChatRef`` encoding.

Full Discord end-to-end tests that exercise the
``collab_bot_added``/``collab_bot_removed`` handler shape are deferred
to Phase 8 (T039–T047) where they piggyback on the handler-signature
refactor in Phase 3.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import discord
import pytest

from messaging.base import ChatRef, LifecycleAdded, LifecycleRemoved
from messaging.discord import DiscordPlatform


@pytest.fixture
def platform():
    return DiscordPlatform(
        bot_token="t",
        guild_id=111,
        owner_id=42,
        control_channel_id=444,
    )


@pytest.fixture
def platform_with_client(platform):
    client = MagicMock()
    client.user = MagicMock(id=9001)
    platform.set_bot(client)
    return platform


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Replace 1s/2s/4s with negligible sleeps so the retry tests run
    in milliseconds. The contract under test is the retry COUNT and
    ordering, not the wall-clock cadence."""
    monkeypatch.setattr(
        DiscordPlatform, "_AUDIT_LOG_BACKOFF", (0.0, 0.0, 0.0),
    )


def _audit_log_entry(user_id: int, user_name: str = "alice"):
    entry = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.name = user_name  # MagicMock treats name= in ctor specially; set explicitly.
    entry.user = user
    return entry


def _async_iter(entries):
    async def _gen():
        for e in entries:
            yield e
    return _gen()


def _make_guild(
    *,
    audit_log=None,
    audit_log_exc=None,
    system_channel=None,
    text_channels=None,
    name="My Guild",
    guild_id=10101,
    leave_coro=None,
):
    """Build a MagicMock that behaves like a discord.Guild for the
    adapter's lifecycle code paths."""
    guild = MagicMock()
    guild.id = guild_id
    guild.name = name
    me = MagicMock()
    guild.me = me

    def audit_logs(*args, **kwargs):
        if audit_log_exc is not None:
            raise audit_log_exc
        return _async_iter(audit_log or [])

    guild.audit_logs = audit_logs
    guild.system_channel = system_channel
    guild.text_channels = text_channels or []
    guild.leave = AsyncMock(side_effect=leave_coro) if leave_coro else AsyncMock()
    return guild


def _make_channel(channel_id: int, *, writable: bool = True):
    ch = MagicMock()
    ch.id = channel_id
    perms = MagicMock()
    perms.send_messages = writable
    ch.permissions_for = MagicMock(return_value=perms)
    return ch


# ── bot_user_id ────────────────────────────────────────────────────────


class TestBotUserId:
    def test_returns_none_before_set_bot(self, platform):
        assert platform.bot_user_id is None

    def test_returns_client_user_id(self, platform_with_client):
        assert platform_with_client.bot_user_id == 9001

    def test_returns_none_when_client_has_no_user(self, platform):
        client = MagicMock()
        client.user = None
        platform.set_bot(client)
        assert platform.bot_user_id is None


# ── _resolve_inviter ──────────────────────────────────────────────────


class TestResolveInviter:
    async def test_success_returns_user_id_and_name(self, platform):
        guild = _make_guild(audit_log=[_audit_log_entry(7777, "alice")])
        uid, name = await platform._resolve_inviter(guild)
        assert uid == 7777
        assert name == "alice"

    async def test_forbidden_returns_none_no_retry(
        self, platform, monkeypatch,
    ):
        call_count = {"n": 0}

        async def _no_sleep(_):
            call_count["sleeps"] = call_count.get("sleeps", 0) + 1

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        guild = _make_guild(
            audit_log_exc=discord.Forbidden(MagicMock(), "no perm"),
        )
        uid, name = await platform._resolve_inviter(guild)
        assert (uid, name) == (None, None)
        # Forbidden short-circuits the retry loop — no sleeps consumed.
        assert call_count.get("sleeps", 0) == 0

    async def test_empty_audit_log_exhausts_retries(self, platform):
        guild = _make_guild(audit_log=[])
        uid, name = await platform._resolve_inviter(guild)
        assert (uid, name) == (None, None)

    async def test_empty_then_success(self, platform):
        # The audit_logs callable is invoked once per attempt — return
        # empty twice, then a real entry on attempt 3.
        guild = MagicMock()
        guild.id = 10101
        attempts = {"n": 0}

        def audit_logs(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return _async_iter([])
            return _async_iter([_audit_log_entry(8888, "bob")])

        guild.audit_logs = audit_logs
        uid, name = await platform._resolve_inviter(guild)
        assert uid == 8888
        assert name == "bob"
        assert attempts["n"] == 3

    async def test_transient_error_then_success(self, platform):
        guild = MagicMock()
        guild.id = 10101
        attempts = {"n": 0}

        def audit_logs(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient blip")
            return _async_iter([_audit_log_entry(9999, "carol")])

        guild.audit_logs = audit_logs
        uid, name = await platform._resolve_inviter(guild)
        assert uid == 9999
        assert name == "carol"

    async def test_all_attempts_error_returns_none(self, platform):
        guild = MagicMock()
        guild.id = 10101

        def audit_logs(*args, **kwargs):
            raise RuntimeError("boom")

        guild.audit_logs = audit_logs
        uid, name = await platform._resolve_inviter(guild)
        assert (uid, name) == (None, None)

    async def test_retry_count_matches_backoff_length(
        self, platform, monkeypatch,
    ):
        """Concrete attempt-count contract: 3 backoff values → up to 3
        attempts when the audit log is consistently empty."""
        attempts = {"n": 0}

        def audit_logs(*args, **kwargs):
            attempts["n"] += 1
            return _async_iter([])

        guild = MagicMock()
        guild.id = 10101
        guild.audit_logs = audit_logs
        await platform._resolve_inviter(guild)
        assert attempts["n"] == len(DiscordPlatform._AUDIT_LOG_BACKOFF)


# ── _pick_writable_channel ────────────────────────────────────────────


class TestPickWritableChannel:
    def test_prefers_system_channel(self, platform):
        sys_ch = _make_channel(1, writable=True)
        other = _make_channel(2, writable=True)
        guild = _make_guild(system_channel=sys_ch, text_channels=[other])
        assert platform._pick_writable_channel(guild) is sys_ch

    def test_falls_back_to_first_writable_text_channel(self, platform):
        unwritable_sys = _make_channel(1, writable=False)
        unwritable_text = _make_channel(2, writable=False)
        writable_text = _make_channel(3, writable=True)
        another = _make_channel(4, writable=True)
        guild = _make_guild(
            system_channel=unwritable_sys,
            text_channels=[unwritable_text, writable_text, another],
        )
        assert platform._pick_writable_channel(guild) is writable_text

    def test_returns_none_when_nothing_writable(self, platform):
        unwritable = _make_channel(1, writable=False)
        guild = _make_guild(
            system_channel=None,
            text_channels=[unwritable, _make_channel(2, writable=False)],
        )
        assert platform._pick_writable_channel(guild) is None

    def test_returns_none_when_guild_me_missing(self, platform):
        guild = MagicMock()
        guild.me = None
        assert platform._pick_writable_channel(guild) is None


# ── register_lifecycle / on_guild_join / on_guild_remove ─────────────


class TestRegisterLifecycle:
    def test_register_lifecycle_attaches_handlers(self, platform):
        client = MagicMock()
        # discord.py's @client.event decorator just returns the function;
        # MagicMock replays this implicitly.
        client.event = lambda f: f
        platform.register_lifecycle(client)
        # No assertion needed — the call must not raise.

    async def test_on_guild_join_emits_lifecycle_added(self, platform):
        """End-to-end through the registered callback: a guild join with
        a writable channel and a successful audit-log lookup dispatches
        a LifecycleAdded event carrying the canonical Discord chat_id."""
        recorded: list[LifecycleAdded] = []

        async def capture(evt):
            recorded.append(evt)

        platform.on_added = capture

        # Capture the registered on_guild_join callback so we can invoke
        # it directly without a live discord.py event loop.
        captured = {}
        client = MagicMock()

        def fake_event(fn):
            captured[fn.__name__] = fn
            return fn

        client.event = fake_event
        platform.register_lifecycle(client)
        assert "on_guild_join" in captured

        ch = _make_channel(channel_id=222, writable=True)
        guild = _make_guild(
            audit_log=[_audit_log_entry(7777, "alice")],
            system_channel=ch,
            guild_id=111,
        )
        await captured["on_guild_join"](guild)

        assert len(recorded) == 1
        event = recorded[0]
        assert event.chat_ref == ChatRef("discord", "111:222")
        assert event.chat_title == "My Guild"
        assert event.added_by_id == 7777
        assert event.added_by_name == "alice"
        assert event.raw_event is guild

    async def test_on_guild_join_no_writable_channel_leaves_guild(
        self, platform,
    ):
        platform.on_added = AsyncMock()
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)

        guild = _make_guild(
            audit_log=[_audit_log_entry(7777, "alice")],
            system_channel=None,
            text_channels=[],
        )
        await captured["on_guild_join"](guild)

        # No writable channel → no LifecycleAdded dispatch + leave is called.
        platform.on_added.assert_not_called()
        guild.leave.assert_awaited_once()

    async def test_on_guild_join_no_handler_set_is_noop(self, platform):
        platform.on_added = None  # explicit
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)

        ch = _make_channel(222, writable=True)
        guild = _make_guild(
            audit_log=[_audit_log_entry(7777, "alice")],
            system_channel=ch,
        )
        # No on_added wired — must not raise.
        await captured["on_guild_join"](guild)

    async def test_on_guild_join_audit_log_failed_added_by_none(
        self, platform,
    ):
        """When audit-log lookup fails, ``added_by_id`` is None on the
        emitted event — the handler will surface the fallback message
        and leave the workspace pending (Phase 3 + Phase 4 work)."""
        recorded: list[LifecycleAdded] = []

        async def capture(evt):
            recorded.append(evt)

        platform.on_added = capture
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)

        ch = _make_channel(222, writable=True)
        guild = _make_guild(
            audit_log_exc=discord.Forbidden(MagicMock(), "no perm"),
            system_channel=ch,
        )
        await captured["on_guild_join"](guild)
        assert len(recorded) == 1
        assert recorded[0].added_by_id is None
        assert recorded[0].added_by_name is None

    async def test_on_guild_join_handler_exception_does_not_crash(
        self, platform, caplog,
    ):
        async def boom(_evt):
            raise RuntimeError("handler is broken")

        platform.on_added = boom
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)

        ch = _make_channel(222, writable=True)
        guild = _make_guild(
            audit_log=[_audit_log_entry(7, "x")],
            system_channel=ch,
        )
        # Must not raise; error logged.
        await captured["on_guild_join"](guild)

    async def test_on_guild_remove_emits_lifecycle_removed(self, platform):
        recorded: list[LifecycleRemoved] = []

        async def capture(evt):
            recorded.append(evt)

        platform.on_removed = capture
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)

        guild = _make_guild(guild_id=555, name="Gone Guild")
        await captured["on_guild_remove"](guild)
        assert len(recorded) == 1
        event = recorded[0]
        # chat_id carries the guild prefix with channel=0 as sentinel.
        assert event.chat_ref == ChatRef("discord", "555:0")
        assert event.chat_title == "Gone Guild"
        assert event.raw_event is guild

    async def test_on_guild_remove_no_handler_is_noop(self, platform):
        platform.on_removed = None
        captured = {}
        client = MagicMock()
        client.event = lambda fn: captured.setdefault(fn.__name__, fn) or fn
        platform.register_lifecycle(client)
        guild = _make_guild(guild_id=555)
        await captured["on_guild_remove"](guild)
