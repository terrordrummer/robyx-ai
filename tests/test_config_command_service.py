import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import config_command_service as service_module
from config_command_service import ConfigCommandService
from config_updates import ConfigPreflightError, ConfigRollbackError
from i18n import STRINGS


@pytest.fixture
def platform():
    result = AsyncMock()
    result.reply = AsyncMock()
    result.send_message = AsyncMock()
    return result


@pytest.fixture
def message():
    return MagicMock(chat_id=-100, thread_id=42)


def _service(tmp_path, restart):
    return ConfigCommandService(
        project_root=lambda: tmp_path,
        restart_service=restart,
        strings=STRINGS,
        logger=logging.getLogger("test.config-command"),
    )


@pytest.mark.asyncio
async def test_ordinary_text_is_not_consumed(tmp_path, platform, message):
    restart = MagicMock()
    service = _service(tmp_path, restart)

    assert await service.handle_owner_update("hello", platform, message, object()) is False

    platform.reply.assert_not_awaited()
    restart.assert_not_called()


@pytest.mark.asyncio
async def test_sensitive_assignment_is_redacted_and_consumed(
    tmp_path, platform, caplog,
):
    secret = "must-never-appear"
    service = _service(tmp_path, MagicMock())

    handled = await service.reject_sensitive_assignment(
        "ROBYX_BOT_TOKEN=%s" % secret,
        platform,
        object(),
    )

    assert handled is True
    assert "ROBYX_BOT_TOKEN" in platform.reply.await_args.args[1]
    assert secret not in platform.reply.await_args.args[1]
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_local_only_parser_boundary_remains_fail_closed(
    tmp_path, platform, message,
):
    service = _service(tmp_path, MagicMock())

    assert await service.reject_sensitive_assignment(
        "ordinary conversation",
        platform,
        object(),
    ) is False
    handled = await service.handle_owner_update(
        "ROBYX_BOT_TOKEN=secret",
        platform,
        message,
        object(),
    )

    assert handled is True
    assert "ROBYX_BOT_TOKEN" in platform.reply.await_args.args[1]


@pytest.mark.asyncio
async def test_success_preserves_reply_send_restart_order(
    tmp_path, platform, message, monkeypatch,
):
    calls = []
    restart = MagicMock(side_effect=lambda: calls.append("restart"))
    service = _service(tmp_path, restart)
    monkeypatch.setattr(
        service_module,
        "apply_env_updates_transactionally",
        lambda *_args, **_kwargs: calls.append("apply"),
    )
    platform.reply.side_effect = lambda *_args, **_kwargs: calls.append("reply")
    platform.send_message.side_effect = lambda **_kwargs: calls.append("send")

    handled = await service.handle_owner_update(
        "SCHEDULER_INTERVAL=120",
        platform,
        message,
        object(),
    )

    assert handled is True
    assert calls == ["apply", "reply", "send", "restart"]
    restart.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reply_key"),
    [
        (ConfigRollbackError("rollback"), "config_rollback_failed"),
        (ConfigPreflightError("preflight"), "config_preflight_failed"),
        (OSError("disk"), "config_write_failed"),
    ],
)
async def test_transaction_failures_are_consumed_without_restart(
    tmp_path, platform, message, monkeypatch, error, reply_key,
):
    restart = MagicMock()
    service = _service(tmp_path, restart)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(service_module, "apply_env_updates_transactionally", fail)

    handled = await service.handle_owner_update(
        "SCHEDULER_INTERVAL=120",
        platform,
        message,
        object(),
    )

    assert handled is True
    assert platform.reply.await_args.args[1] == STRINGS[reply_key]
    platform.send_message.assert_not_awaited()
    restart.assert_not_called()
