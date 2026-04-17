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


class TestSilentMarker:
    @pytest.mark.asyncio
    async def test_silent_marker_suppresses_delivery_on_success(
        self, tmp_path, mock_platform
    ):
        output_log = tmp_path / "output.log"
        output_log.write_text("[SILENT]")

        backend = MagicMock()
        backend.parse_response.return_value = {"text": "[SILENT]"}

        task = {
            "name": "personal-assistant-check",
            "description": "Personal assistant proactive check",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task, output_log, mock_platform, backend, 0, MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_marker_with_status_lines_still_suppressed(
        self, tmp_path, mock_platform
    ):
        output_log = tmp_path / "output.log"
        output_log.write_text("[STATUS scanning todos]\n[SILENT]")

        backend = MagicMock()
        backend.parse_response.return_value = {
            "text": "[STATUS scanning todos]\n[SILENT]",
        }

        task = {
            "name": "personal-assistant-check",
            "description": "Personal assistant proactive check",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task, output_log, mock_platform, backend, 0, MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_marker_does_not_suppress_failures(
        self, tmp_path, mock_platform
    ):
        output_log = tmp_path / "output.log"
        output_log.write_text("[SILENT]")

        backend = MagicMock()
        backend.parse_response.return_value = {"text": "[SILENT]"}

        task = {
            "name": "system-monitor",
            "description": "System monitor",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task, output_log, mock_platform, backend, 1, MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_awaited_once()
        sent_text = mock_platform.send_to_channel.await_args.args[1]
        assert "Task failed" in sent_text or "exit code" in sent_text

    @pytest.mark.asyncio
    async def test_real_content_with_silent_substring_still_delivered(
        self, tmp_path, mock_platform
    ):
        output_log = tmp_path / "output.log"
        output_log.write_text("[SILENT]\nActually I found a problem: disk full")

        backend = MagicMock()
        backend.parse_response.return_value = {
            "text": "[SILENT]\nActually I found a problem: disk full",
        }

        task = {
            "name": "system-monitor",
            "description": "System monitor",
            "thread_id": "903",
        }

        ok = await deliver_task_output(
            task, output_log, mock_platform, backend, 0, MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_awaited_once()
        sent_text = mock_platform.send_to_channel.await_args.args[1]
        assert "disk full" in sent_text


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


class TestContinuousMacroScrubbing:
    """Feature 004 regression: scheduled subprocess output must NEVER deliver
    a raw continuous-task macro to the chat. The scheduler has no
    interactive agent context, so it MUST strip (never dispatch) any tokens
    it sees."""

    @pytest.mark.asyncio
    async def test_scheduled_reply_strips_macro(self, tmp_path, mock_platform):
        output_log = tmp_path / "out.log"
        output_log.write_text("ignored; the backend parser returns its own text")

        backend = MagicMock()
        backend.parse_response.return_value = {
            "text": (
                "Job done.\n\n"
                '[CREATE_CONTINUOUS name="stray" work_dir="/tmp/x"]\n'
                '[CONTINUOUS_PROGRAM]\n'
                '{"objective":"x","success_criteria":["y"],'
                '"first_step":{"number":1,"description":"z"}}\n'
                '[/CONTINUOUS_PROGRAM]'
            )
        }

        task = {
            "name": "nightly-run",
            "description": "Nightly run",
            "thread_id": 101,
        }

        ok = await deliver_task_output(
            task, output_log, mock_platform, backend, 0, MagicMock(),
        )

        assert ok is True
        mock_platform.send_to_channel.assert_awaited()
        delivered = mock_platform.send_to_channel.await_args.args[1]
        assert "[CREATE_CONTINUOUS" not in delivered
        assert "CONTINUOUS_PROGRAM" not in delivered.upper()
        assert "Job done" in delivered
