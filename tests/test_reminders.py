"""Tests for bot/reminders.py — append_reminder helper and engine wiring."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest


class TestAppendReminder:
    def test_creates_file_when_missing(self, tmp_path):
        from reminders import append_reminder

        f = tmp_path / "reminders.json"
        assert not f.exists()

        entry = {
            "id": "r-abc12345",
            "message": "hello",
            "fire_at": "2026-04-09T10:00:00+00:00",
            "thread_id": 903,
            "created_at": "2026-04-08T12:00:00+00:00",
            "status": "pending",
        }
        append_reminder(f, entry)

        assert f.exists()
        data = json.loads(f.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0] == entry

    def test_appends_to_existing_file(self, tmp_path):
        from reminders import append_reminder

        f = tmp_path / "reminders.json"
        first = {"id": "r-1", "message": "a", "fire_at": "x", "status": "pending"}
        f.write_text(json.dumps([first]))

        second = {"id": "r-2", "message": "b", "fire_at": "y", "status": "pending"}
        append_reminder(f, second)

        data = json.loads(f.read_text())
        assert len(data) == 2
        assert data[0] == first
        assert data[1] == second

    def test_atomic_rewrite_no_temp_left_behind(self, tmp_path):
        """append_reminder must use the temp+rename idiom and not leave the
        ``.tmp`` file behind on success."""
        from reminders import append_reminder

        f = tmp_path / "reminders.json"
        entry = {"id": "r-1", "message": "a", "fire_at": "x", "status": "pending"}
        append_reminder(f, entry)

        leftover = f.with_suffix(f.suffix + ".tmp")
        assert not leftover.exists()

    def test_unicode_preserved(self, tmp_path):
        from reminders import append_reminder

        f = tmp_path / "reminders.json"
        entry = {"id": "r-1", "message": "⏰ caffè ☕", "status": "pending"}
        append_reminder(f, entry)

        data = json.loads(f.read_text())
        assert data[0]["message"] == "⏰ caffè ☕"


class TestSystemPromptsHaveRemindersSection:
    """Guard against future drift: every system prompt that an interactive
    agent can run under must include the universal `## Reminders` section."""

    def test_robyx_prompt_has_reminders(self):
        from config import ROBYX_SYSTEM_PROMPT

        assert "## Reminders" in ROBYX_SYSTEM_PROMPT
        assert "[REMIND" in ROBYX_SYSTEM_PROMPT

    def test_workspace_prompt_has_reminders(self):
        from config import WORKSPACE_AGENT_SYSTEM_PROMPT

        assert "## Reminders" in WORKSPACE_AGENT_SYSTEM_PROMPT
        assert "[REMIND" in WORKSPACE_AGENT_SYSTEM_PROMPT

    def test_focused_prompt_has_reminders(self):
        from config import FOCUSED_AGENT_SYSTEM_PROMPT

        assert "## Reminders" in FOCUSED_AGENT_SYSTEM_PROMPT
        assert "[REMIND" in FOCUSED_AGENT_SYSTEM_PROMPT


class TestCheckReminders:
    @pytest.mark.asyncio
    async def test_due_reminder_uses_platform_send_message(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-1",
            "chat_id": "C123",
            "thread_id": "171234.5678",
            "message": "hello",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})

        await check_reminders(reminders_file, platform)

        platform.send_message.assert_awaited_once_with(
            chat_id="C123",
            text="hello",
            thread_id="171234.5678",
            parse_mode="markdown",
        )
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "sent"
        assert "sent_at" in data[0]

    @pytest.mark.asyncio
    async def test_legacy_entry_without_chat_id_uses_default_fallback(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-legacy",
            "thread_id": 903,
            "message": "legacy",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})

        await check_reminders(reminders_file, platform, default_chat_id=-100999)

        platform.send_message.assert_awaited_once_with(
            chat_id=-100999,
            text="legacy",
            thread_id=903,
            parse_mode="markdown",
        )
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_missing_destination_marks_reminder_invalid(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-bad",
            "message": "nowhere",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock()

        await check_reminders(reminders_file, platform)

        platform.send_message.assert_not_awaited()
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "invalid"

    @pytest.mark.asyncio
    async def test_none_return_keeps_reminder_pending_for_retry(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-retry",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "retry me",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value=None)

        await check_reminders(reminders_file, platform)

        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_send_exception_resets_claim_back_to_pending(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-boom",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "boom",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(side_effect=RuntimeError("network down"))

        await check_reminders(reminders_file, platform)

        platform.send_message.assert_awaited_once()
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "pending"
        assert "claim_token" not in data[0]
        assert "claimed_at" not in data[0]
        assert "sent_at" not in data[0]

    @pytest.mark.asyncio
    async def test_append_during_send_is_preserved(self, tmp_path):
        from reminders import append_reminder, check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-due",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "send first",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        async def send_message(**kwargs):
            append_reminder(reminders_file, {
                "id": "r-new",
                "chat_id": -100999,
                "thread_id": 903,
                "message": "queued while sending",
                "fire_at": "2999-01-01T00:00:00+00:00",
                "status": "pending",
            })
            return {"ok": True}

        platform = AsyncMock()
        platform.send_message = AsyncMock(side_effect=send_message)

        await check_reminders(reminders_file, platform)

        data = json.loads(reminders_file.read_text())
        by_id = {item["id"]: item for item in data}
        assert by_id["r-due"]["status"] == "sent"
        assert by_id["r-new"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_concurrent_checks_do_not_double_send_same_reminder(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-once",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "only once",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
        }]))

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(**kwargs):
            started.set()
            await release.wait()
            return {"ok": True}

        first_platform = AsyncMock()
        first_platform.send_message = AsyncMock(side_effect=slow_send)

        second_platform = AsyncMock()
        second_platform.send_message = AsyncMock(return_value={"ok": True})

        first_run = asyncio.create_task(check_reminders(reminders_file, first_platform))
        await started.wait()

        await check_reminders(reminders_file, second_platform)

        second_platform.send_message.assert_not_awaited()
        release.set()
        await first_run

        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "sent"
        first_platform.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded_marks_reminder_failed(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-exhaust",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "never gonna send",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
            "attempts": 10,
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})

        await check_reminders(reminders_file, platform)

        platform.send_message.assert_not_awaited()
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_attempts_incremented_on_each_claim(self, tmp_path):
        from reminders import check_reminders

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-count",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "count me",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "pending",
            "attempts": 3,
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})

        await check_reminders(reminders_file, platform)

        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "sent"
        assert data[0]["attempts"] == 4

    @pytest.mark.asyncio
    async def test_stale_sending_claim_is_retried(self, tmp_path):
        from reminders import check_reminders

        old_claim = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps([{
            "id": "r-stale",
            "chat_id": -100999,
            "thread_id": 903,
            "message": "retry stale claim",
            "fire_at": "2000-01-01T00:00:00+00:00",
            "status": "sending",
            "claim_token": "old-claim",
            "claimed_at": old_claim,
        }]))

        platform = AsyncMock()
        platform.send_message = AsyncMock(return_value={"ok": True})

        await check_reminders(reminders_file, platform)

        platform.send_message.assert_awaited_once()
        data = json.loads(reminders_file.read_text())
        assert data[0]["status"] == "sent"
        assert "claim_token" not in data[0]
        assert "claimed_at" not in data[0]
