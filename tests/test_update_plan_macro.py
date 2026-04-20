"""Tests for ``bot/update_plan_macro.py`` — 0.24.0.

Covers:
  - Pure extraction: paired / unclosed / fenced / mixed-quote forms.
  - Field validation: accepted fields and bad-type rejections.
  - Apply: success, not-found, workspace-scoping, partial overrides,
    plan_text override, no-op.
  - Defense-in-depth scrubbing via ``strip_update_plan_macros`` and the
    ``strip_control_tokens_for_user`` chokepoint in ``continuous_macro``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import update_plan_macro as upm  # noqa: E402
from update_plan_macro import (  # noqa: E402
    UpdatePlanContext,
    UpdatePlanOutcome,
    UpdatePlanTokens,
    apply_update_plan_macros,
    extract_update_plan_macros,
    strip_update_plan_macros,
)


# ─────────────────────────────────────────────────────────────────────────
# Extraction — paired and unpaired forms
# ─────────────────────────────────────────────────────────────────────────


class TestExtraction:
    def test_simple_paired(self):
        text = (
            'Before.\n'
            '[UPDATE_PLAN name="zeus-research"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"checkpoint_policy": "on-milestone"}\n'
            '[/CONTINUOUS_PROGRAM]\n'
            'After.\n'
        )
        stripped, toks = extract_update_plan_macros(text)
        assert len(toks) == 1
        assert toks[0].name_raw == "zeus-research"
        assert '"checkpoint_policy"' in toks[0].program_raw
        assert "Before." in stripped
        assert "After." in stripped
        assert "UPDATE_PLAN" not in stripped
        assert "CONTINUOUS_PROGRAM" not in stripped

    def test_missing_program_block(self):
        text = '[UPDATE_PLAN name="x"]\n\nNothing follows.\n'
        _, toks = extract_update_plan_macros(text)
        assert len(toks) == 1
        assert toks[0].program_span is None
        assert toks[0].open_span is not None

    def test_unclosed_program(self):
        text = (
            '[UPDATE_PLAN name="x"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective": "something"\n'
        )
        _, toks = extract_update_plan_macros(text)
        assert len(toks) == 1
        assert toks[0].unclosed_program is True

    def test_curly_quotes(self):
        text = (
            '[UPDATE_PLAN name=\u201Czeus\u201D]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"objective": "new"}\n'
            '[/CONTINUOUS_PROGRAM]\n'
        )
        _, toks = extract_update_plan_macros(text)
        assert len(toks) == 1
        assert toks[0].name_raw == "zeus"

    def test_multiple_macros(self):
        text = (
            '[UPDATE_PLAN name="a"]\n[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
            '---\n'
            '[UPDATE_PLAN name="b"]\n[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
        )
        stripped, toks = extract_update_plan_macros(text)
        assert [t.name_raw for t in toks] == ["a", "b"]
        assert "UPDATE_PLAN" not in stripped

    def test_code_fence_wrapper_stripped(self):
        text = (
            'Header.\n'
            '```\n'
            '[UPDATE_PLAN name="x"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{}\n'
            '[/CONTINUOUS_PROGRAM]\n'
            '```\n'
            'Footer.\n'
        )
        stripped, toks = extract_update_plan_macros(text)
        assert len(toks) == 1
        assert "```" not in stripped
        assert "Header." in stripped
        assert "Footer." in stripped

    def test_idempotent(self):
        text = (
            '[UPDATE_PLAN name="x"]\n[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
        )
        once, _ = extract_update_plan_macros(text)
        twice, toks = extract_update_plan_macros(once)
        assert twice == once
        assert toks == []


# ─────────────────────────────────────────────────────────────────────────
# Field validation
# ─────────────────────────────────────────────────────────────────────────


class TestValidation:
    def test_accepts_known_fields(self):
        overrides, bad = upm._validate_overrides({
            "objective": "new obj",
            "success_criteria": ["a", "b"],
            "constraints": ["no breaking"],
            "checkpoint_policy": "on-milestone",
            "context": "extra notes",
            "plan_text": "# Free-form\n",
        })
        assert bad is None
        assert overrides["objective"] == "new obj"
        assert overrides["success_criteria"] == ["a", "b"]
        assert overrides["constraints"] == ["no breaking"]
        assert overrides["checkpoint_policy"] == "on-milestone"
        assert overrides["context"] == "extra notes"
        assert overrides["plan_text"].startswith("# Free-form")

    def test_rejects_empty_objective(self):
        _, bad = upm._validate_overrides({"objective": ""})
        assert bad == "objective"

    def test_rejects_non_list_criteria(self):
        _, bad = upm._validate_overrides({"success_criteria": "a,b"})
        assert bad == "success_criteria"

    def test_rejects_non_string_criterion(self):
        _, bad = upm._validate_overrides({"success_criteria": ["ok", 42]})
        assert bad == "success_criteria"

    def test_rejects_invalid_policy(self):
        _, bad = upm._validate_overrides({"checkpoint_policy": "whenever"})
        assert bad == "checkpoint_policy"

    def test_accepts_every_allowed_policy(self):
        for p in ("on-demand", "on-uncertainty", "on-milestone", "every-N-steps"):
            overrides, bad = upm._validate_overrides({"checkpoint_policy": p})
            assert bad is None
            assert overrides["checkpoint_policy"] == p

    def test_ignores_unknown_fields(self):
        overrides, bad = upm._validate_overrides({
            "objective": "ok",
            "foo": "bar",  # Forward-compatibility: ignored, not an error.
        })
        assert bad is None
        assert "foo" not in overrides
        assert overrides["objective"] == "ok"

    def test_empty_program_is_allowed(self):
        overrides, bad = upm._validate_overrides({})
        assert bad is None
        assert overrides == {}


# ─────────────────────────────────────────────────────────────────────────
# Apply — success and error paths
# ─────────────────────────────────────────────────────────────────────────


class _FakeStateStore:
    """In-memory state store for hermetic apply tests."""

    def __init__(self, states: dict[str, dict]):
        self.states = states
        self.writes: list[tuple[str, dict]] = []
        self.plans: dict[str, str] = {}

    def reader(self, name: str):
        state = self.states.get(name)
        return ("/fake/%s/state.json" % name, state)

    def writer(self, path, state: dict):
        self.writes.append((str(path), state))
        # Also update the canonical map so later reads see the change.
        name = state.get("name") or Path(str(path)).parent.name
        self.states[name] = state

    def plan_writer(self, name: str, body: str):
        self.plans[name] = body


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestApply:
    def _state(self, name: str = "zeus", thread_id: int = 42):
        return {
            "name": name,
            "workspace_thread_id": thread_id,
            "status": "paused",
            "program": {
                "objective": "old obj",
                "success_criteria": ["c1"],
                "constraints": [],
                "checkpoint_policy": "on-demand",
                "context": "",
            },
            "history": [],
            "total_steps_completed": 0,
        }

    def test_applies_partial_override(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"checkpoint_policy": "on-milestone", '
            '"success_criteria": ["new-c1", "new-c2"]}\n'
            '[/CONTINUOUS_PROGRAM]\n'
        )
        out, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "applied"
        assert outcomes[0].name == "zeus"
        # Merged: objective preserved, policy + criteria overridden.
        written = store.states["zeus"]
        assert written["program"]["objective"] == "old obj"
        assert written["program"]["checkpoint_policy"] == "on-milestone"
        assert written["program"]["success_criteria"] == ["new-c1", "new-c2"]
        # plan.md regenerated from merged program.
        assert "on-milestone" in store.plans["zeus"]
        assert "new-c1" in store.plans["zeus"]

    def test_plan_text_override_wins_over_renderer(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"plan_text": "# Custom plan\\n\\nhuman-authored body\\n"}\n'
            '[/CONTINUOUS_PROGRAM]\n'
        )
        out, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "applied"
        assert store.plans["zeus"].startswith("# Custom plan")
        assert "human-authored body" in store.plans["zeus"]
        # Program fields untouched.
        assert store.states["zeus"]["program"]["objective"] == "old obj"

    def test_task_not_found_is_reported(self):
        store = _FakeStateStore({})  # empty
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="ghost"]\n'
            '[CONTINUOUS_PROGRAM]\n{"objective": "x"}\n[/CONTINUOUS_PROGRAM]\n'
        )
        out, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "rejected"
        assert outcomes[0].reason == "not_found"
        assert "ghost" in out
        assert not store.writes

    def test_workspace_scoping_blocks_cross_workspace_edit(self):
        # Task belongs to workspace thread 999 but the invoking agent is on 42.
        store = _FakeStateStore({"zeus": self._state(thread_id=999)})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n{"objective": "hijack"}\n[/CONTINUOUS_PROGRAM]\n'
        )
        _, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "rejected"
        assert outcomes[0].reason == "not_found"
        assert not store.writes
        # Original state untouched.
        assert store.states["zeus"]["program"]["objective"] == "old obj"

    def test_bad_json_is_rejected(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n{not valid json}\n[/CONTINUOUS_PROGRAM]\n'
        )
        _, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "rejected"
        assert outcomes[0].reason == "bad_json"
        assert not store.writes

    def test_bad_field_is_rejected(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n'
            '{"checkpoint_policy": "whenever-i-feel-like"}\n'
            '[/CONTINUOUS_PROGRAM]\n'
        )
        _, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "rejected"
        assert outcomes[0].reason == "bad_field"
        assert outcomes[0].detail == "checkpoint_policy"
        assert not store.writes

    def test_missing_program_block_is_rejected(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = '[UPDATE_PLAN name="zeus"]\n\n(nothing else)\n'
        _, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "rejected"
        assert outcomes[0].reason == "malformed_missing_program"
        assert not store.writes

    def test_empty_overrides_is_noop_success(self):
        store = _FakeStateStore({"zeus": self._state()})
        ctx = UpdatePlanContext(
            thread_id=42,
            state_reader=store.reader,
            state_writer=store.writer,
            plan_writer=store.plan_writer,
        )
        text = (
            '[UPDATE_PLAN name="zeus"]\n'
            '[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
        )
        _, outcomes = _run(apply_update_plan_macros(text, ctx))
        assert outcomes[0].outcome == "applied"
        assert not store.writes  # nothing to persist


# ─────────────────────────────────────────────────────────────────────────
# Strip
# ─────────────────────────────────────────────────────────────────────────


class TestStrip:
    def test_strip_update_plan_macros(self):
        text = (
            'Before.\n'
            '[UPDATE_PLAN name="x"]\n[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
            'After.\n'
        )
        stripped, count = strip_update_plan_macros(text)
        assert count == 1
        assert "UPDATE_PLAN" not in stripped
        assert "CONTINUOUS_PROGRAM" not in stripped
        assert "Before." in stripped
        assert "After." in stripped

    def test_strip_control_tokens_for_user_scrubs_update_plan_too(self):
        from continuous_macro import strip_control_tokens_for_user
        text = (
            'Hi.\n'
            '[UPDATE_PLAN name="x"]\n[CONTINUOUS_PROGRAM]\n{}\n[/CONTINUOUS_PROGRAM]\n'
            'Bye.\n'
        )
        out = strip_control_tokens_for_user(text)
        assert "UPDATE_PLAN" not in out
        assert "CONTINUOUS_PROGRAM" not in out
        assert "Hi." in out
        assert "Bye." in out
