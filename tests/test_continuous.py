"""Tests for bot/continuous.py — continuous task state management."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config as cfg
import continuous as cont_mod
from continuous import (
    build_step_context,
    check_rate_limit_recovery,
    complete_task,
    create_continuous_task,
    is_ready_for_next_step,
    load_state,
    mark_step_completed,
    mark_step_failed,
    mark_step_started,
    pause_task,
    resume_task,
    save_state,
    set_awaiting_input,
    set_next_step,
    set_rate_limited,
    state_file_path,
)


@pytest.fixture(autouse=True)
def _patch_continuous_paths(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    cont_dir = data_dir / "continuous"
    monkeypatch.setattr(cont_mod, "CONTINUOUS_DIR", cont_dir)
    cont_dir.mkdir(parents=True, exist_ok=True)


# ── State I/O ────────────────────────────────────────────────────────────────


class TestStateIO:
    def test_load_missing_returns_none(self, tmp_path):
        assert load_state(tmp_path / "nonexistent.json") is None

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = {"name": "test", "status": "pending"}
        save_state(path, state)
        loaded = load_state(path)
        assert loaded == state

    def test_atomic_write(self, tmp_path):
        path = tmp_path / "state.json"
        save_state(path, {"x": 1})
        assert not path.with_suffix(".tmp").exists()

    def test_corrupt_json(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("NOT JSON")
        assert load_state(path) is None


# ── State creation ───────────────────────────────────────────────────────────


class TestCreateContinuousTask:
    def test_creates_state_file(self):
        state = create_continuous_task(
            name="improve-psf",
            parent_workspace="astro-project",
            program={
                "objective": "Improve PSF deconvolution",
                "success_criteria": ["PSNR > 35dB"],
                "constraints": ["Don't modify public API"],
                "checkpoint_policy": "every-5-steps",
                "context": "Using FFT-based approach",
                "first_step": {"number": 1, "description": "Profile current performance"},
            },
            thread_id=999,
            branch="continuous/improve-psf",
            work_dir="/tmp/project",
        )

        assert state["name"] == "improve-psf"
        assert state["status"] == "pending"
        assert state["branch"] == "continuous/improve-psf"
        assert state["program"]["objective"] == "Improve PSF deconvolution"
        assert state["next_step"]["number"] == 1
        assert state["history"] == []

        # State file should exist
        path = state_file_path("improve-psf")
        assert path.exists()
        loaded = load_state(path)
        assert loaded["name"] == "improve-psf"


# ── Step lifecycle ───────────────────────────────────────────────────────────


class TestStepLifecycle:
    def _base_state(self):
        return {
            "name": "test",
            "status": "pending",
            "current_step": None,
            "next_step": {"number": 1, "description": "Do step 1"},
            "history": [],
            "total_steps_completed": 0,
            "updated_at": "",
        }

    def test_mark_step_started(self):
        state = self._base_state()
        mark_step_started(state, 1, "Do step 1")
        assert state["status"] == "running"
        assert state["current_step"]["number"] == 1
        assert state["current_step"]["status"] == "running"
        assert "started_at" in state["current_step"]

    def test_mark_step_completed(self):
        state = self._base_state()
        mark_step_started(state, 1, "Do step 1")
        mark_step_completed(state, "commit abc123", 120)
        assert state["current_step"] is None
        assert len(state["history"]) == 1
        assert state["history"][0]["artifact"] == "commit abc123"
        assert state["total_steps_completed"] == 1

    def test_mark_step_failed(self):
        state = self._base_state()
        mark_step_started(state, 1, "Do step 1")
        mark_step_failed(state, "subprocess crashed")
        assert state["status"] == "error"
        assert state["current_step"]["status"] == "failed"
        assert state["current_step"]["error"] == "subprocess crashed"

    def test_set_next_step(self):
        state = self._base_state()
        state["total_steps_completed"] = 3
        set_next_step(state, "Do step 4")
        assert state["next_step"]["number"] == 4
        assert state["next_step"]["description"] == "Do step 4"


# ── Status transitions ───────────────────────────────────────────────────────


class TestStatusTransitions:
    def _base_state(self):
        return {
            "status": "running",
            "rate_limited_until": None,
            "updated_at": "",
        }

    def test_pause(self):
        state = self._base_state()
        pause_task(state)
        assert state["status"] == "paused"

    def test_resume(self):
        state = self._base_state()
        state["status"] = "paused"
        resume_task(state)
        assert state["status"] == "pending"

    def test_complete(self):
        state = self._base_state()
        state["current_step"] = {"number": 5}
        state["next_step"] = {"number": 6}
        complete_task(state)
        assert state["status"] == "completed"
        assert state["current_step"] is None
        assert state["next_step"] is None

    def test_awaiting_input(self):
        state = self._base_state()
        set_awaiting_input(state, "Which approach should I use?")
        assert state["status"] == "awaiting-input"
        assert state["awaiting_question"] == "Which approach should I use?"

    def test_rate_limited(self):
        state = self._base_state()
        set_rate_limited(state, retry_after_seconds=3600)
        assert state["status"] == "rate-limited"
        assert state["rate_limited_until"] is not None

    def test_rate_limit_recovery_not_yet(self):
        state = self._base_state()
        set_rate_limited(state, retry_after_seconds=3600)
        assert check_rate_limit_recovery(state) is False

    def test_rate_limit_recovery_expired(self):
        state = self._base_state()
        state["status"] = "rate-limited"
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state["rate_limited_until"] = past
        assert check_rate_limit_recovery(state) is True


# ── Query helpers ────────────────────────────────────────────────────────────


class TestQueryHelpers:
    def test_is_ready_pending_with_next_step(self):
        state = {
            "status": "pending",
            "next_step": {"number": 1, "description": "Start"},
        }
        assert is_ready_for_next_step(state) is True

    def test_is_ready_completed(self):
        state = {"status": "completed", "next_step": None}
        assert is_ready_for_next_step(state) is False

    def test_is_ready_paused(self):
        state = {"status": "paused", "next_step": {"number": 1, "description": "x"}}
        assert is_ready_for_next_step(state) is False

    def test_is_ready_running(self):
        state = {"status": "running", "next_step": {"number": 1, "description": "x"}}
        assert is_ready_for_next_step(state) is False

    def test_is_ready_no_next_step(self):
        state = {"status": "pending", "next_step": None}
        assert is_ready_for_next_step(state) is False

    def test_build_step_context_empty(self):
        state = {"history": []}
        assert build_step_context(state) == "(no previous steps)"

    def test_build_step_context_with_history(self):
        state = {
            "history": [
                {"step": 1, "description": "Did thing", "artifact": "commit abc"},
                {"step": 2, "description": "Did another", "artifact": "commit def"},
            ]
        }
        ctx = build_step_context(state)
        assert "Step 1" in ctx
        assert "Step 2" in ctx
        assert "commit abc" in ctx
