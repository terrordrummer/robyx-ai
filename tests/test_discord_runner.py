"""Integration tests for the discord.py event bridge in ``bot.bot``."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from runtime_supervisor import RuntimeSupervisor


class _FakeIntents:
    @classmethod
    def default(cls):
        return cls()


class _FakeDiscordClient:
    def __init__(self, *, intents):
        self.intents = intents
        self.events = {}
        self.user = SimpleNamespace(id=999_000, name="robyx")
        self.run_token = None
        self.native_close = AsyncMock()
        self.close = self.native_close

    def event(self, callback):
        self.events[callback.__name__] = callback
        return callback

    def run(self, token):
        self.run_token = token


def _wire_fake_runner(monkeypatch, handlers=None):
    client = _FakeDiscordClient(intents=_FakeIntents.default())
    fake_discord = SimpleNamespace(
        Intents=_FakeIntents,
        Client=lambda **kwargs: client,
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    platform = MagicMock()
    platform.control_room_id = 700
    platform.set_bot = MagicMock()
    platform.register_lifecycle = MagicMock()
    platform.is_owner = MagicMock(side_effect=lambda uid: uid == 101)

    h = handlers or {"message": AsyncMock(), "voice": AsyncMock()}
    bot._run_discord(platform, h, MagicMock(), MagicMock())
    return client, platform, h


def _message(user_id, text="hello", *, guild_id=111, channel_id=222):
    return SimpleNamespace(
        author=SimpleNamespace(
            id=user_id,
            name="user-%d" % user_id,
            display_name="User %d" % user_id,
        ),
        guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id),
        attachments=[],
        flags=SimpleNamespace(voice=False),
        content=text,
    )


@pytest.mark.asyncio
async def test_owner_operator_and_participant_reach_message_handler(monkeypatch):
    """The runner must not enforce owner-only before role authorization."""
    client, platform, handlers = _wire_fake_runner(monkeypatch)
    on_message = client.events["on_message"]

    # These ids represent an owner, operator, and participant respectively.
    for user_id in (101, 202, 303):
        await on_message(_message(user_id))

    assert handlers["message"].await_count == 3
    dispatched = [call.args[1] for call in handlers["message"].await_args_list]
    assert [msg.user_id for msg in dispatched] == [101, 202, 303]
    assert {msg.chat_id for msg in dispatched} == {"111:222"}
    assert {msg.thread_id for msg in dispatched} == {222}
    # Authorization belongs to handlers, not this transport bridge.
    platform.is_owner.assert_not_called()


@pytest.mark.asyncio
async def test_manual_claim_command_receives_canonical_chat_id(monkeypatch):
    claim = AsyncMock()
    client, _, _ = _wire_fake_runner(
        monkeypatch,
        handlers={
            "message": AsyncMock(),
            "voice": AsyncMock(),
            "im-the-owner": claim,
        },
    )

    await client.events["on_message"](
        _message(303, "/im-the-owner atlas", guild_id=444, channel_id=555),
    )

    claim.assert_awaited_once()
    msg = claim.await_args.args[1]
    assert msg.chat_id == "444:555"
    assert msg.thread_id == 555
    assert msg.args == ["atlas"]


@pytest.mark.asyncio
async def test_clear_command_reaches_clear_handler(monkeypatch):
    clear = AsyncMock()
    client, _, _ = _wire_fake_runner(
        monkeypatch,
        handlers={
            "message": AsyncMock(),
            "voice": AsyncMock(),
            "clear": clear,
        },
    )

    await client.events["on_message"](
        _message(101, "/clear", guild_id=444, channel_id=555),
    )

    clear.assert_awaited_once()
    msg = clear.await_args.args[1]
    assert msg.chat_id == "444:555"
    assert msg.thread_id == 555


@pytest.mark.asyncio
async def test_ready_reconnect_does_not_duplicate_boot_or_loops(monkeypatch):
    stop = asyncio.Event()
    calls = {"boot": 0, "scheduler": 0, "updates": 0}

    async def boot_once(*args):
        calls["boot"] += 1

    async def scheduler_loop(*args):
        calls["scheduler"] += 1
        await stop.wait()

    async def update_loop(*args):
        calls["updates"] += 1
        await stop.wait()

    monkeypatch.setattr(bot, "_run_boot_sequence", boot_once)
    monkeypatch.setattr(bot, "_background_scheduler_loop", scheduler_loop)
    monkeypatch.setattr(bot, "_background_update_loop", update_loop)

    client, _, _ = _wire_fake_runner(monkeypatch)
    on_ready = client.events["on_ready"]

    await on_ready()
    await asyncio.sleep(0)
    await on_ready()  # discord.py emits this again after a reconnect
    await asyncio.sleep(0)

    assert calls == {"boot": 1, "scheduler": 1, "updates": 1}

    stop.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_client_close_drains_discord_background_tasks(monkeypatch):
    supervisor = RuntimeSupervisor()
    cancelled = {"scheduler": False, "updates": False}

    async def boot_once(*args):
        return None

    def long_loop(name):
        async def _loop(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled[name] = True
        return _loop

    monkeypatch.setattr(bot, "get_runtime_supervisor", lambda: supervisor)
    monkeypatch.setattr(bot, "_run_boot_sequence", boot_once)
    monkeypatch.setattr(bot, "_background_scheduler_loop", long_loop("scheduler"))
    monkeypatch.setattr(bot, "_background_update_loop", long_loop("updates"))

    client, _, _ = _wire_fake_runner(monkeypatch)
    native_close = client.native_close
    await client.events["on_ready"]()
    await asyncio.sleep(0)

    await client.close()

    native_close.assert_awaited_once_with()
    assert cancelled == {"scheduler": True, "updates": True}
    assert supervisor.task_count == 0
