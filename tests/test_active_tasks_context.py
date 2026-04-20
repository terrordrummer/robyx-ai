"""Tests for ``ai_invoke._render_active_continuous_tasks`` — 0.24.0.

The workspace agent's system prompt must be aware of the continuous
tasks it owns so it refuses to create duplicates and routes scope
changes through ``[UPDATE_PLAN]``. This helper renders a short,
workspace-scoped block appended to the system prompt at invocation time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


def _write_state(dir_: Path, name: str, **overrides):
    state = {
        "name": name,
        "workspace_thread_id": overrides.get("workspace_thread_id", 42),
        "status": overrides.get("status", "pending"),
        "program": {
            "objective": overrides.get("objective", "Obj for %s" % name),
            "success_criteria": overrides.get("success_criteria", []),
            "constraints": overrides.get("constraints", []),
            "checkpoint_policy": overrides.get("checkpoint_policy", "on-demand"),
            "context": overrides.get("context", ""),
        },
        "current_step": None,
        "next_step": overrides.get("next_step", {
            "number": 3, "description": "continue where we left off",
        }),
        "history": [],
        "total_steps_completed": 0,
    }
    if "awaiting_question" in overrides:
        state["awaiting_question"] = overrides["awaiting_question"]
    d = dir_ / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(state))
    return state


class TestRenderActiveContinuousTasks:
    def test_empty_when_no_tasks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CONTINUOUS_DIR", tmp_path / "continuous")
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks(42)
        assert out == ""

    def test_empty_when_thread_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CONTINUOUS_DIR", tmp_path / "continuous")
        import ai_invoke
        assert ai_invoke._render_active_continuous_tasks(None) == ""
        assert ai_invoke._render_active_continuous_tasks("") == ""
        assert ai_invoke._render_active_continuous_tasks("-") == ""

    def test_renders_single_active_task(self, tmp_path, monkeypatch):
        continuous_dir = tmp_path / "continuous"
        continuous_dir.mkdir()
        _write_state(
            continuous_dir, "zeus-research",
            workspace_thread_id=42, status="awaiting-input",
            awaiting_question="OK to modify the stacker?",
            checkpoint_policy="on-milestone",
            objective="Improve deconvolution benchmark",
        )
        monkeypatch.setattr("config.CONTINUOUS_DIR", continuous_dir)
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks(42)
        assert "Active continuous tasks" in out
        assert "zeus-research" in out
        assert "awaiting-input" in out
        assert "on-milestone" in out
        assert "stacker" in out  # awaiting question surfaced
        # Must mention UPDATE_PLAN as the right macro for in-place edits.
        assert "UPDATE_PLAN" in out

    def test_filters_by_workspace_thread(self, tmp_path, monkeypatch):
        continuous_dir = tmp_path / "continuous"
        continuous_dir.mkdir()
        _write_state(
            continuous_dir, "mine", workspace_thread_id=42, status="pending",
        )
        _write_state(
            continuous_dir, "theirs", workspace_thread_id=999, status="pending",
        )
        monkeypatch.setattr("config.CONTINUOUS_DIR", continuous_dir)
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks(42)
        assert "mine" in out
        assert "theirs" not in out

    def test_excludes_terminal_states(self, tmp_path, monkeypatch):
        continuous_dir = tmp_path / "continuous"
        continuous_dir.mkdir()
        _write_state(
            continuous_dir, "done", workspace_thread_id=42, status="completed",
        )
        _write_state(
            continuous_dir, "crashed", workspace_thread_id=42, status="error",
        )
        _write_state(
            continuous_dir, "live", workspace_thread_id=42, status="pending",
        )
        monkeypatch.setattr("config.CONTINUOUS_DIR", continuous_dir)
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks(42)
        assert "live" in out
        assert "done" not in out
        assert "crashed" not in out

    def test_includes_paused_and_rate_limited(self, tmp_path, monkeypatch):
        continuous_dir = tmp_path / "continuous"
        continuous_dir.mkdir()
        _write_state(
            continuous_dir, "paused-task",
            workspace_thread_id=42, status="paused",
        )
        _write_state(
            continuous_dir, "rl-task",
            workspace_thread_id=42, status="rate-limited",
        )
        monkeypatch.setattr("config.CONTINUOUS_DIR", continuous_dir)
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks(42)
        assert "paused-task" in out
        assert "rl-task" in out

    def test_normalises_string_thread_ids(self, tmp_path, monkeypatch):
        continuous_dir = tmp_path / "continuous"
        continuous_dir.mkdir()
        # State stores int 42; invocation arrives with string "42".
        _write_state(
            continuous_dir, "t", workspace_thread_id=42, status="pending",
        )
        monkeypatch.setattr("config.CONTINUOUS_DIR", continuous_dir)
        import ai_invoke
        out = ai_invoke._render_active_continuous_tasks("42")
        assert "`t`" in out

    def test_handles_missing_continuous_dir_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CONTINUOUS_DIR", tmp_path / "does-not-exist")
        import ai_invoke
        assert ai_invoke._render_active_continuous_tasks(42) == ""
