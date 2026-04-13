"""Tests for bot/scheduled_delivery.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduled_delivery import deliver_task_output, start_task_delivery_watch


class TestDeliverTaskOutput:
    @pytest.mark.asyncio
    async def test_sends_parsed_result_to_target_channel(self, tmp_path, mock_platform):
        output_log = tmp_path / "output.log"
        output_log.write_text('{"result":"Run finished successfully."}\n')

        backend = MagicMock()
        backend.parse_response.return_value = {"text": "Run finished successfully."}

        task = {
            "name": "nightly-cleanup",
            "description": "Nightly cleanup",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task,
            output_log,
            mock_platform,
            backend,
            0,
            MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_awaited_once()
        args = mock_platform.send_to_channel.await_args.args
        kwargs = mock_platform.send_to_channel.await_args.kwargs
        assert args[0] == 903
        assert "Run finished successfully." in args[1]
        assert kwargs["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_posts_fallback_message_when_run_has_no_visible_output(
        self, tmp_path, mock_platform
    ):
        output_log = tmp_path / "output.log"
        output_log.write_text("")

        backend = MagicMock()
        backend.parse_response.return_value = {"text": ""}

        task = {
            "name": "nightly-cleanup",
            "description": "Nightly cleanup",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task,
            output_log,
            mock_platform,
            backend,
            0,
            MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_awaited_once()
        sent_text = mock_platform.send_to_channel.await_args.args[1]
        assert "Nightly cleanup" in sent_text
        assert "did not produce any visible output" in sent_text

    @pytest.mark.asyncio
    async def test_retries_plain_text_after_markdown_failure(self, tmp_path, mock_platform):
        output_log = tmp_path / "output.log"
        output_log.write_text("plain result")

        backend = MagicMock()
        backend.parse_response.return_value = "plain result"
        mock_platform.send_to_channel = AsyncMock(side_effect=[False, True])

        task = {
            "name": "report",
            "description": "Daily report",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task,
            output_log,
            mock_platform,
            backend,
            0,
            MagicMock(),
        )

        assert ok is True
        assert mock_platform.send_to_channel.await_count == 2
        first = mock_platform.send_to_channel.await_args_list[0]
        second = mock_platform.send_to_channel.await_args_list[1]
        assert first.kwargs["parse_mode"] == "Markdown"
        assert second.kwargs["parse_mode"] == ""


class TestStartTaskDeliveryWatch:
    @pytest.mark.asyncio
    async def test_waits_for_process_and_cleans_lock(self, tmp_path):
        lock_file = tmp_path / "lock"
        lock_file.write_text("123")
        output_log = tmp_path / "output.log"
        output_log.write_text("done")

        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        backend = MagicMock()
        backend.parse_response.return_value = "done"

        platform = AsyncMock()
        platform.send_to_channel = AsyncMock(return_value=True)
        platform.max_message_length = 4000

        watch = start_task_delivery_watch(
            {"name": "scheduled", "thread_id": "903"},
            proc,
            output_log,
            lock_file,
            platform,
            backend,
            MagicMock(),
        )

        assert watch is not None
        await watch

        proc.wait.assert_awaited_once()
        assert not lock_file.exists()
        platform.send_to_channel.assert_awaited()
