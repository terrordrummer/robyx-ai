"""Tests for spec 006 structured delivery header.

Contract: ``specs/006-continuous-task-robustness/contracts/delivery-header.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture
def sd():
    import scheduled_delivery  # type: ignore
    return scheduled_delivery


# ── Regex shape ────────────────────────────────────────────────────────────


def test_header_regex_matches_canonical_example(sd):
    line = "🔄 [zeus-research] · Step 12 · ⏸ awaiting input · 14:31"
    match = sd.DELIVERY_HEADER_RE.match(line)
    assert match is not None
    assert match.group("name") == "zeus-research"
    assert match.group("step") == "12"
    assert match.group("state_label").strip() == "awaiting input"
    assert match.group("hhmm") == "14:31"


def test_header_regex_supports_step_counter_with_total(sd):
    line = "🔄 [zeus-rd-172] · Step 17/30 · ▶ running · 06:48"
    assert sd.DELIVERY_HEADER_RE.match(line) is not None


def test_header_regex_rejects_non_canonical(sd):
    for bad in (
        "🔄 [foo] running",                    # missing structure
        "[foo] · Step 1 · ▶ running · 14:31",  # no icon
        "🔄 [FOO] · Step 1 · ▶ running · 14:31",  # uppercase name
        "🔄 [foo] · Step abc · ▶ running · 14:31",  # non-numeric step
    ):
        assert sd.DELIVERY_HEADER_RE.match(bad) is None, bad


# ── _state_presentation ────────────────────────────────────────────────────


def test_state_presentation_covers_every_canonical(sd):
    for status in (
        "pending", "running", "awaiting_input", "rate_limited",
        "stopped", "completed", "error",
    ):
        emoji, label = sd._state_presentation(status)
        assert emoji
        assert label


def test_state_presentation_legacy_forms(sd):
    emoji_new, label_new = sd._state_presentation("awaiting_input")
    emoji_old, label_old = sd._state_presentation("awaiting-input")
    assert (emoji_new, label_new) == (emoji_old, label_old)

    emoji_new, label_new = sd._state_presentation("rate_limited")
    emoji_old, label_old = sd._state_presentation("rate-limited")
    assert (emoji_new, label_new) == (emoji_old, label_old)

    emoji_stop, label_stop = sd._state_presentation("stopped")
    emoji_paus, label_paus = sd._state_presentation("paused")
    assert (emoji_stop, label_stop) == (emoji_paus, label_paus)


def test_state_presentation_override_for_special_events(sd):
    emoji, label = sd._state_presentation("running", override="workspace_closed")
    assert emoji == "⚠"
    assert label == "workspace closed"


# ── _build_continuous_header ──────────────────────────────────────────────


def test_build_header_running_with_step_and_next(sd):
    state = {
        "status": "running",
        "current_step": {"number": 5, "description": "current step desc"},
        "next_step": {"number": 6, "description": "next planned step desc"},
    }
    header, next_line = sd._build_continuous_header(
        "zeus-research", state, hhmm="10:00",
    )
    assert sd.DELIVERY_HEADER_RE.match(header) is not None
    assert "zeus-research" in header
    assert "Step 5" in header
    assert "▶" in header
    assert next_line.startswith("→ Next:")
    assert "next planned step desc" in next_line


def test_build_header_next_truncated_with_ellipsis(sd):
    long_desc = "x" * 200
    state = {
        "status": "running",
        "current_step": {"number": 1},
        "next_step": {"description": long_desc},
    }
    _, next_line = sd._build_continuous_header(
        "t", state, hhmm="10:00",
    )
    assert next_line.endswith("…")
    # Cap check: header prefix "→ Next: " = 8 chars, then up to 80 chars of
    # description (79 + ellipsis).
    assert len(next_line) <= len("→ Next: ") + 80


def test_build_header_awaiting_input(sd):
    state = {
        "status": "awaiting_input",
        "current_step": {"number": 12},
        "awaiting_question": "pick topic",
    }
    header, _ = sd._build_continuous_header("zr", state, hhmm="14:31")
    assert "⏸" in header
    assert "awaiting input" in header


def test_build_header_rate_limited_with_until_hhmm(sd):
    until = datetime(2026, 4, 22, 15, 42, tzinfo=timezone.utc).isoformat()
    state = {
        "status": "rate_limited",
        "current_step": {"number": 3},
        "rate_limited_until": until,
    }
    header, _ = sd._build_continuous_header("t", state, hhmm="14:42")
    assert "rate-limited until 15:42" in header


def test_build_header_completed_suppresses_next(sd):
    state = {
        "status": "completed",
        "current_step": {"number": 12},
        "next_step": {"description": "ignored"},
    }
    header, next_line = sd._build_continuous_header(
        "t", state, state_override="completed", hhmm="18:00",
    )
    assert "✅" in header
    assert next_line is None


def test_build_header_workspace_closed_override(sd):
    state = {"status": "running", "current_step": {"number": 5}}
    header, _ = sd._build_continuous_header(
        "t", state, state_override="workspace_closed", hhmm="17:05",
    )
    assert "⚠" in header
    assert "workspace closed" in header


def test_build_header_no_state_falls_back_to_running(sd):
    header, next_line = sd._build_continuous_header("t", None, hhmm="12:00")
    assert "▶" in header
    assert "Step 0" in header  # no step info → 0
    assert next_line is None


# ── _render_result_message ─────────────────────────────────────────────────


def test_render_message_continuous_has_header(sd, tmp_path, monkeypatch):
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="sample",
        parent_workspace="ws",
        program={"objective": "x"},
        thread_id=1,
        branch="b",
        work_dir="/tmp",
    )
    # Place at step 3 mid-run.
    state["current_step"] = {"number": 3, "description": "x"}
    state["status"] = "running"
    cont.save_state(cont.state_file_path("sample"), state)

    task = {
        "type": "continuous", "name": "sample",
        "description": "Sample continuous task",
    }
    msg = sd._render_result_message(
        task, "Hello world.", returncode=0, raw_output="",
    )
    first_line = msg.split("\n", 1)[0]
    assert sd.DELIVERY_HEADER_RE.match(first_line) is not None
    assert "Hello world." in msg


def test_render_message_strips_embedded_agent_header(sd, tmp_path, monkeypatch):
    """If an agent emits its own header, the renderer strips it before
    prepending the canonical header (no double-headers).
    """
    import continuous as cont
    monkeypatch.setattr(cont, "CONTINUOUS_DIR", tmp_path / "continuous")

    state = cont.create_continuous_task(
        name="sample2", parent_workspace="ws",
        program={"objective": "x"},
        thread_id=1, branch="b", work_dir="/tmp",
    )
    state["current_step"] = {"number": 3}
    state["status"] = "running"
    cont.save_state(cont.state_file_path("sample2"), state)

    task = {"type": "continuous", "name": "sample2"}

    agent_output = (
        "🔄 [sample2] · Step 3 · ▶ running · 09:00\n"
        "\n"
        "Real body content."
    )
    msg = sd._render_result_message(
        task, agent_output, returncode=0, raw_output="",
    )
    # Exactly one header line (the canonical one).
    header_count = 0
    for line in msg.splitlines():
        if sd.DELIVERY_HEADER_RE.match(line.strip()):
            header_count += 1
    assert header_count == 1
    assert "Real body content." in msg


def test_render_message_non_continuous_unchanged(sd):
    """Non-continuous tasks keep the legacy icon+name format."""
    task = {"type": "periodic", "name": "backup", "description": "Backup"}
    msg = sd._render_result_message(
        task, "Backup done.", returncode=0, raw_output="",
    )
    # No structured header on periodic — just the icon + name prefix.
    assert sd.DELIVERY_HEADER_RE.match(msg.split("\n")[0]) is None
    assert "⏰" in msg
    assert "[backup]" in msg
