"""Tests for the unified delivery marker (spec 005, US3).

The marker is applied in a single chokepoint (`scheduled_delivery.format_delivery_message`)
consumed by two call sites:
  - `scheduled_delivery._render_result_message` (agent-driven tasks: continuous,
    periodic, one-shot)
  - `scheduler._dispatch_reminders` (reminder path — no LLM)

These tests exercise each task type, the unknown-type fallback, the
long-name truncation rule, and the invariant that conversational replies
are never marked by the delivery layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_platform():
    p = AsyncMock()
    p.send_to_channel = AsyncMock(return_value=True)
    p.send_message = AsyncMock(return_value=MagicMock())
    p.max_message_length = 4000
    return p


# ─────────────────────────────────────────────────────────────────────────
# format_delivery_message — pure unit tests
# ─────────────────────────────────────────────────────────────────────────


class TestFormatDeliveryMessage:
    def test_continuous_marker(self):
        from scheduled_delivery import format_delivery_message
        out = format_delivery_message("continuous", "daily-report", "Step 3 done")
        assert out == "🔄 [daily-report] Step 3 done"

    def test_periodic_marker(self):
        from scheduled_delivery import format_delivery_message
        out = format_delivery_message("periodic", "check-metrics", "All nominal.")
        assert out == "⏰ [check-metrics] All nominal."

    def test_oneshot_marker_all_aliases_resolve_to_same_icon(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("one-shot", "x", "b").startswith("📌 [x]")
        assert format_delivery_message("oneshot", "x", "b").startswith("📌 [x]")
        assert format_delivery_message("one_shot", "x", "b").startswith("📌 [x]")

    def test_reminder_marker(self):
        from scheduled_delivery import format_delivery_message
        out = format_delivery_message("reminder", "standup", "fra 5 min")
        assert out == "🔔 [standup] fra 5 min"

    def test_unknown_type_returns_body_unchanged_and_logs_warning(self, caplog):
        from scheduled_delivery import format_delivery_message
        with caplog.at_level(logging.WARNING, logger="robyx.scheduled_delivery"):
            out = format_delivery_message("magical-task", "xyz", "body text")
        assert out == "body text"
        assert any("magical-task" in rec.getMessage() for rec in caplog.records)

    def test_empty_body_omits_space_and_uses_bare_marker(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("continuous", "task", "") == "🔄 [task]"
        assert format_delivery_message("continuous", "task", None) == "🔄 [task]"

    def test_none_body_is_treated_as_empty(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("periodic", "x", None) == "⏰ [x]"

    def test_case_insensitive_task_type(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("CONTINUOUS", "x", "b") == "🔄 [x] b"
        assert format_delivery_message("Reminder", "y", "b") == "🔔 [y] b"

    def test_whitespace_padded_task_type(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("  continuous  ", "x", "b") == "🔄 [x] b"

    def test_long_name_truncated_to_64_chars_with_ellipsis(self):
        from scheduled_delivery import format_delivery_message
        long_name = "a" * 128
        out = format_delivery_message("continuous", long_name, "body")
        # Marker contains 64 chars of name followed by an ellipsis.
        assert out.startswith("🔄 [")
        inside = out.split("[", 1)[1].split("]", 1)[0]
        assert len(inside) == 64
        assert inside.endswith("…")

    def test_empty_name_renders_fallback_question_mark(self):
        from scheduled_delivery import format_delivery_message
        assert format_delivery_message("continuous", "", "body") == "🔄 [?] body"
        assert format_delivery_message("continuous", "   ", "body") == "🔄 [?] body"


# ─────────────────────────────────────────────────────────────────────────
# _render_result_message — delivery chokepoint integration per task type
# ─────────────────────────────────────────────────────────────────────────


class TestRenderResultMessageMarkers:
    def test_continuous_task_gets_rocket_icon(self):
        from scheduled_delivery import _render_result_message
        task = {"name": "daily-report", "type": "continuous"}
        out = _render_result_message(task, "Step 3 done.", 0, "")
        assert out.startswith("🔄 [daily-report]")

    def test_periodic_task_gets_alarm_icon(self):
        from scheduled_delivery import _render_result_message
        task = {"name": "check-metrics", "type": "periodic"}
        out = _render_result_message(task, "All nominal.", 0, "")
        assert out.startswith("⏰ [check-metrics]")

    def test_oneshot_task_gets_pin_icon(self):
        from scheduled_delivery import _render_result_message
        task = {"name": "deploy-staging", "type": "one-shot"}
        out = _render_result_message(task, "Deploy done.", 0, "")
        assert out.startswith("📌 [deploy-staging]")

    def test_unknown_type_on_entry_falls_back_to_continuous_default(self):
        # _render_result_message defaults task_type to "continuous" when
        # the queue entry has no type field (legacy pre-0.23.0 entries).
        from scheduled_delivery import _render_result_message
        task = {"name": "legacy"}
        out = _render_result_message(task, "ok", 0, "")
        assert out.startswith("🔄 [legacy]")


# ─────────────────────────────────────────────────────────────────────────
# _dispatch_reminders — reminder path integration
# ─────────────────────────────────────────────────────────────────────────


class TestReminderDispatchMarker:
    @pytest.mark.asyncio
    async def test_reminder_send_uses_bell_marker(
        self, tmp_path, monkeypatch, mock_platform,
    ):
        import scheduler as sched
        # Point queue writes at a tmp path so _reconcile doesn't touch real state.
        monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "queue.json")
        (tmp_path / "queue.json").write_text('{"entries": []}')

        reminder = {
            "id": "r-abc",
            "claim_token": "ct-1",
            "chat_id": 123,
            "thread_id": 456,
            "message": "Check the deploy",
            "late_seconds": 0,
        }

        await sched._dispatch_reminders([reminder], mock_platform, default_chat_id=None)

        mock_platform.send_message.assert_awaited_once()
        kwargs = mock_platform.send_message.await_args.kwargs
        assert kwargs["text"].startswith("🔔 [promemoria] ")
        assert "Check the deploy" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_reminder_with_explicit_name_uses_that_name(
        self, tmp_path, monkeypatch, mock_platform,
    ):
        import scheduler as sched
        monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "queue.json")
        (tmp_path / "queue.json").write_text('{"entries": []}')

        reminder = {
            "id": "r-abc",
            "claim_token": "ct-1",
            "chat_id": 123,
            "thread_id": 456,
            "message": "Remember the milk",
            "name": "shopping",
            "late_seconds": 0,
        }

        await sched._dispatch_reminders([reminder], mock_platform)

        kwargs = mock_platform.send_message.await_args.kwargs
        assert kwargs["text"].startswith("🔔 [shopping] ")


# ─────────────────────────────────────────────────────────────────────────
# Conversational replies are NOT marked by the delivery layer
# ─────────────────────────────────────────────────────────────────────────


class TestConversationalRepliesUnmarked:
    """Spec FR-005 invariant: the marker is applied ONLY by the delivery
    chokepoint; primary agent's interactive responses go through
    `handlers._send_response` which uses `strip_control_tokens_for_user`
    (not `format_delivery_message`) — so no marker is prepended.
    """

    def test_strip_control_tokens_does_not_add_marker(self):
        from continuous_macro import strip_control_tokens_for_user
        text = "Hello, world!"
        assert strip_control_tokens_for_user(text) == text
        for icon in ("🔄", "⏰", "📌", "🔔"):
            assert icon not in strip_control_tokens_for_user(text)


# ─────────────────────────────────────────────────────────────────────────
# Idempotency invariant — only TWO call sites invoke format_delivery_message
# ─────────────────────────────────────────────────────────────────────────


class TestSingleChokepointInvariant:
    """Static check: `format_delivery_message` may only be called from the
    two expected sites — `scheduled_delivery._render_result_message` and
    `scheduler._dispatch_reminders`. Any other call would risk double-
    marking or out-of-band marking of non-scheduled output.
    """

    def test_only_two_call_sites_in_bot_package(self):
        bot_dir = Path(__file__).resolve().parent.parent / "bot"
        call_sites: list[tuple[str, int]] = []
        for py in bot_dir.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                # Strip comments so the self-reference inside docstrings /
                # descriptive comments doesn't trip the check.
                code_only = line.split("#", 1)[0]
                if "format_delivery_message(" in code_only:
                    call_sites.append((py.name, lineno))

        # Definition also matches the literal; filter it out.
        actual_calls = [
            (fn, ln) for (fn, ln) in call_sites
            if not (fn == "scheduled_delivery.py" and _is_definition(bot_dir, ln))
        ]

        call_files = sorted({fn for (fn, _) in actual_calls})
        assert call_files == ["scheduled_delivery.py", "scheduler.py"], (
            "format_delivery_message must only be called from "
            "scheduled_delivery._render_result_message and "
            "scheduler._dispatch_reminders. Found unexpected call sites: "
            "%r" % actual_calls
        )


def _is_definition(bot_dir: Path, lineno: int) -> bool:
    text = (bot_dir / "scheduled_delivery.py").read_text().splitlines()
    line = text[lineno - 1] if 0 <= lineno - 1 < len(text) else ""
    return line.lstrip().startswith("def format_delivery_message")
