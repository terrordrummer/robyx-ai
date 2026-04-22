"""Tests for bot/lifecycle_macros.py (spec 005, US2).

Exercises the six lifecycle macros (LIST_TASKS, TASK_STATUS, STOP_TASK,
PAUSE_TASK, RESUME_TASK, GET_PLAN), workspace scoping, and the
disambiguation flow against an injected in-memory queue + state reader.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import lifecycle_macros as lm
from lifecycle_macros import (
    DispatchContext,
    MacroInvocation,
    handle_lifecycle_macros,
    parse_lifecycle_macros,
    render_list,
    render_status,
    scope_to_workspace,
    substitute_macros,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers & fixtures
# ─────────────────────────────────────────────────────────────────────────


def _entry(
    name: str,
    type_: str,
    thread_id: Any = 42,
    status: str = "pending",
    **extra,
) -> dict:
    e = {
        "id": "id-" + name,
        "name": name,
        "type": type_,
        "status": status,
        # Preserve None/"-" sentinels so scope_to_workspace normalisation
        # is exercised faithfully; coerce integers to string to match
        "thread_id": thread_id if thread_id in (None, "-") else str(thread_id),
    }
    e.update(extra)
    return e


def _state(
    name: str,
    status: str = "running",
    objective: str = "do the thing",
    history=None,
) -> dict:
    return {
        "id": "state-" + name,
        "name": name,
        "status": status,
        "workspace_thread_id": 42,
        "parent_workspace": "ops",
        "program": {
            "objective": objective,
            "success_criteria": ["crit 1"],
            "constraints": ["constraint 1"],
            "checkpoint_policy": "on-demand",
            "context": "",
        },
        "history": history or [],
        "total_steps_completed": len(history or []),
    }


def _ctx(entries, state_map=None, user_message=None):
    state_map = state_map or {}

    def queue_reader():
        return list(entries)

    def state_reader(name):
        return state_map.get(name)

    return DispatchContext(
        chat_id=1,
        thread_id=42,
        queue_reader=queue_reader,
        state_reader=state_reader,
        user_message=user_message,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# Grammar / parsing
# ─────────────────────────────────────────────────────────────────────────


class TestGrammar:
    def test_parse_list_tasks(self):
        out = parse_lifecycle_macros("hey [LIST_TASKS] please")
        assert len(out) == 1
        assert out[0].kind == "list_tasks"
        assert out[0].name is None

    def test_parse_task_status_with_name(self):
        out = parse_lifecycle_macros('foo [TASK_STATUS name="daily-report"] bar')
        assert len(out) == 1
        assert out[0].kind == "task_status"
        assert out[0].name == "daily-report"

    def test_parse_all_mutators(self):
        text = (
            '[STOP_TASK name="a"] [PAUSE_TASK name="b"] '
            '[RESUME_TASK name="c"] [GET_PLAN name="d"]'
        )
        out = parse_lifecycle_macros(text)
        kinds = [m.kind for m in out]
        names = [m.name for m in out]
        assert kinds == ["stop_task", "pause_task", "resume_task", "get_plan"]
        assert names == ["a", "b", "c", "d"]

    def test_parse_case_insensitive(self):
        out = parse_lifecycle_macros("[list_tasks] [Stop_Task name=\"x\"]")
        assert [m.kind for m in out] == ["list_tasks", "stop_task"]

    def test_parse_curly_quotes_accepted(self):
        out = parse_lifecycle_macros('[TASK_STATUS name=\u201Cdaily-report\u201D]')
        assert len(out) == 1
        assert out[0].name == "daily-report"

    def test_parse_empty_text_returns_empty(self):
        assert parse_lifecycle_macros("") == []
        assert parse_lifecycle_macros(None) == []


# ─────────────────────────────────────────────────────────────────────────
# Workspace scoping
# ─────────────────────────────────────────────────────────────────────────


class TestScopeToWorkspace:
    def test_matches_by_thread_id_string_or_int(self):
        entries = [
            _entry("a", "continuous", thread_id=42),
            _entry("b", "continuous", thread_id=99),
            _entry("c", "periodic", thread_id="42"),
        ]
        scoped = scope_to_workspace(entries, chat_id=1, thread_id=42)
        names = sorted(e["name"] for e in scoped)
        assert names == ["a", "c"]

    def test_excludes_entries_from_other_workspaces(self):
        entries = [
            _entry("x", "continuous", thread_id=99),
            _entry("y", "reminder", thread_id="-"),
        ]
        assert scope_to_workspace(entries, chat_id=1, thread_id=42) == []

    def test_handles_missing_and_dash_thread_ids(self):
        entries = [
            _entry("x", "continuous", thread_id=None),
            _entry("y", "continuous", thread_id="-"),
        ]
        assert scope_to_workspace(entries, chat_id=1, thread_id=42) == []
        assert scope_to_workspace(entries, chat_id=1, thread_id=None) == [
            e for e in entries
        ]


# ─────────────────────────────────────────────────────────────────────────
# LIST_TASKS
# ─────────────────────────────────────────────────────────────────────────


class TestListTasks:
    def test_empty_workspace(self):
        ctx = _ctx(entries=[], state_map={})
        out = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation(kind="list_tasks", name=None, span=(0, 0))], ctx,
        ))
        assert list(out.values())[0] == "Nessun task attivo nel workspace."

    def test_grouped_summary_includes_icons_and_names(self):
        entries = [
            _entry("daily-report", "continuous", status="running"),
            _entry("check-metrics", "periodic", status="pending",
                   next_run="2026-04-20T09:00:00Z"),
            _entry("deploy-staging", "one-shot", status="pending",
                   scheduled_at="2026-04-20T10:00:00Z"),
            _entry("remind-abc", "reminder", status="pending",
                   fire_at="2026-04-20T08:55:00Z"),
        ]
        state_map = {"daily-report": _state("daily-report", "running")}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation(kind="list_tasks", name=None, span=(0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        # Grouping order + icons present
        assert body.index("🔄") < body.index("⏰")
        assert body.index("⏰") < body.index("📌")
        assert body.index("📌") < body.index("🔔")
        # Each task name appears
        for name in ("daily-report", "check-metrics", "deploy-staging", "remind-abc"):
            assert ("`%s`" % name) in body
        assert "*Task attivi nel workspace* (4)" in body

    def test_excludes_completed_and_canceled(self):
        entries = [
            _entry("done", "continuous", status="completed"),
            _entry("stopped", "periodic", status="canceled"),
            _entry("active", "continuous", status="running"),
        ]
        state_map = {
            "done": _state("done", "completed"),
            "active": _state("active", "running"),
        }
        ctx = _ctx(entries, state_map)
        out = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation(kind="list_tasks", name=None, span=(0, 0))], ctx,
        ))
        body = list(out.values())[0]
        assert "active" in body
        assert "`done`" not in body
        assert "`stopped`" not in body


# ─────────────────────────────────────────────────────────────────────────
# TASK_STATUS
# ─────────────────────────────────────────────────────────────────────────


class TestTaskStatus:
    def test_single_match_returns_detailed_status(self):
        entries = [_entry("daily-report", "continuous")]
        state_map = {
            "daily-report": _state("daily-report", "running",
                                   objective="Increase docs coverage"),
        }
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("task_status", "daily-report", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "daily-report" in body
        assert "running" in body
        assert "Increase docs coverage" in body
        assert "🔄" in body

    def test_zero_match_returns_not_found(self):
        ctx = _ctx(entries=[_entry("other", "continuous")])
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("task_status", "nonexistent", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "Nessun task attivo" in body
        assert "nonexistent" in body


# ─────────────────────────────────────────────────────────────────────────
# STOP_TASK — with real state mutation via continuous module
# ─────────────────────────────────────────────────────────────────────────


class TestStopTask:
    def test_stop_continuous_transitions_status_to_stopped(
        self, tmp_path, monkeypatch,
    ):
        """Spec 006 FR-014: stop preserves resumability (status=stopped,
        not completed). Prior to spec-006, stop wrote completed which
        conflicted with the distinct complete op."""
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        cont.save_state(
            cont.state_file_path("daily-report"),
            _state("daily-report", "running"),
        )

        queue_file = tmp_path / "queue.json"
        queue_file.write_text('[{"name": "daily-report", '
                              '"type": "continuous", "status": "pending", '
                              '"thread_id": "42"}]')
        monkeypatch.setattr("scheduler.QUEUE_FILE", queue_file)

        entries = [_entry("daily-report", "continuous", status="pending")]
        state_map = {"daily-report": _state("daily-report", "running")}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("stop_task", "daily-report", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "fermato" in body.lower()

        new_state = cont.load_state(cont.state_file_path("daily-report"))
        # Spec 006 canonical "stopped" (resumable), NOT terminal "completed".
        assert new_state["status"] == "stopped"

    def test_stop_non_continuous_cancels_queue_entry(
        self, tmp_path, monkeypatch,
    ):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text('[{"name": "check-metrics", '
                              '"type": "periodic", "status": "pending", '
                              '"thread_id": "42"}]')
        monkeypatch.setattr("scheduler.QUEUE_FILE", queue_file)

        entries = [_entry("check-metrics", "periodic", status="pending")]
        ctx = _ctx(entries)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("stop_task", "check-metrics", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "fermato" in body.lower()

        import json
        data = json.loads(queue_file.read_text())
        assert data[0]["status"] == "canceled"
        assert data[0]["canceled_reason"] == "stopped by user"

    def test_stop_reason_includes_user_message_snippet(
        self, tmp_path, monkeypatch,
    ):
        """When the dispatch context carries the user message, the
        recorded ``canceled_reason`` must include a snippet of it so
        later audits can distinguish user-driven stops from agent-driven
        stops.
        """
        queue_file = tmp_path / "queue.json"
        queue_file.write_text('[{"name": "check-metrics", '
                              '"type": "periodic", "status": "pending", '
                              '"thread_id": "42"}]')
        monkeypatch.setattr("scheduler.QUEUE_FILE", queue_file)

        entries = [_entry("check-metrics", "periodic", status="pending")]
        ctx = _ctx(entries, user_message="ferma il task, non mi serve più")
        asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("stop_task", "check-metrics", (0, 0))], ctx,
        ))

        import json
        data = json.loads(queue_file.read_text())
        assert data[0]["status"] == "canceled"
        reason = data[0]["canceled_reason"]
        assert reason.startswith("stopped by user:")
        assert "ferma il task" in reason


# ─────────────────────────────────────────────────────────────────────────
# PAUSE_TASK / RESUME_TASK
# ─────────────────────────────────────────────────────────────────────────


class TestPauseResume:
    def test_pause_then_resume_continuous(self, tmp_path, monkeypatch):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        state = _state("daily-report", "running")
        cont.save_state(cont.state_file_path("daily-report"), state)

        # PAUSE
        entries = [_entry("daily-report", "continuous")]
        state_map = {"daily-report": state}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("pause_task", "daily-report", (0, 0))], ctx,
        ))
        assert "pausa" in list(subs.values())[0].lower()
        reloaded = cont.load_state(cont.state_file_path("daily-report"))
        # Spec 006: pause writes canonical "stopped" (legacy "paused" is
        # normalised on load anyway, so either would appear here).
        assert reloaded["status"] == "stopped"

        # RESUME
        state_map = {"daily-report": reloaded}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("resume_task", "daily-report", (0, 0))], ctx,
        ))
        assert "ripreso" in list(subs.values())[0].lower()
        reloaded = cont.load_state(cont.state_file_path("daily-report"))
        assert reloaded["status"] == "pending"

    def test_resume_from_awaiting_input_clears_question(
        self, tmp_path, monkeypatch,
    ):
        """Regression for v0.24.2 fire-and-forget bug.

        Pre-v0.24.2 the resume handler accepted only {paused, rate-limited}
        and rejected ``awaiting-input`` with a "non è in pausa" message,
        even though the primary-agent contract told the workspace to emit
        [RESUME_TASK] exactly for that state. The task would then get
        stuck forever. This verifies the whitelist now includes
        ``awaiting-input`` AND that the stale ``awaiting_question`` key
        is cleared when resuming.
        """
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        state = _state("daily-report", "awaiting-input")
        state["awaiting_question"] = "Should I use option A or B?"
        cont.save_state(cont.state_file_path("daily-report"), state)

        entries = [_entry("daily-report", "continuous")]
        state_map = {"daily-report": state}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("resume_task", "daily-report", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "ripreso" in body.lower()

        reloaded = cont.load_state(cont.state_file_path("daily-report"))
        assert reloaded["status"] == "pending"
        assert "awaiting_question" not in reloaded

    def test_pause_periodic_is_unsupported_message(self, tmp_path, monkeypatch):
        entries = [_entry("check-metrics", "periodic")]
        ctx = _ctx(entries)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("pause_task", "check-metrics", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "non supportata" in body.lower()
        assert "ferma" in body.lower()  # suggests stop instead


# ─────────────────────────────────────────────────────────────────────────
# Disambiguation
# ─────────────────────────────────────────────────────────────────────────


class TestDisambiguation:
    def test_ambiguous_substring_triggers_disambiguation_not_mutation(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        # Two tasks both matching "report"
        for name in ("daily-report", "weekly-report"):
            cont.save_state(cont.state_file_path(name), _state(name, "running"))

        entries = [
            _entry("daily-report", "continuous", status="running"),
            _entry("weekly-report", "continuous", status="running"),
        ]
        state_map = {
            "daily-report": _state("daily-report", "running"),
            "weekly-report": _state("weekly-report", "running"),
        }
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("stop_task", "report", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "Quale intendi?" in body
        assert "daily-report" in body
        assert "weekly-report" in body

        # Neither state should have been touched.
        for name in ("daily-report", "weekly-report"):
            reloaded = cont.load_state(cont.state_file_path(name))
            assert reloaded["status"] == "running"

    def test_exact_match_preferred_over_substring(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        for name in ("foo", "foobar"):
            cont.save_state(cont.state_file_path(name), _state(name, "running"))

        # "foo" exactly matches one, substrings another — exact wins.
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("[]")
        monkeypatch.setattr("scheduler.QUEUE_FILE", queue_file)

        entries = [
            _entry("foo", "continuous", status="running"),
            _entry("foobar", "continuous", status="running"),
        ]
        state_map = {
            "foo": _state("foo", "running"),
            "foobar": _state("foobar", "running"),
        }
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("stop_task", "foo", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "fermato" in body.lower()
        assert "foo`" in body  # name reported
        # foobar should NOT have been stopped.
        assert cont.load_state(cont.state_file_path("foobar"))["status"] == "running"


# ─────────────────────────────────────────────────────────────────────────
# GET_PLAN
# ─────────────────────────────────────────────────────────────────────────


class TestGetPlan:
    def test_returns_plan_md_content_for_continuous(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont

        cont.write_plan_md("daily-report", "# Plan: daily-report\n\n## Objective\nX\n")
        entries = [_entry("daily-report", "continuous", status="running")]
        state_map = {"daily-report": _state("daily-report", "running")}
        ctx = _ctx(entries, state_map)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("get_plan", "daily-report", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "# Plan: daily-report" in body
        assert "X" in body

    def test_get_plan_for_non_continuous_returns_friendly_message(
        self, tmp_path, monkeypatch,
    ):
        entries = [_entry("check-metrics", "periodic", status="pending")]
        ctx = _ctx(entries)
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("get_plan", "check-metrics", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "solo per task continuativi" in body.lower() or \
               "continuativi" in body.lower()


# ─────────────────────────────────────────────────────────────────────────
# Workspace isolation
# ─────────────────────────────────────────────────────────────────────────


class TestWorkspaceIsolation:
    def test_other_workspace_tasks_are_invisible(self):
        entries = [
            _entry("my-task", "continuous", thread_id=42, status="running"),
            _entry("their-task", "continuous", thread_id=99, status="running"),
        ]
        # Spec 006: scope filter also consults state.workspace_thread_id
        # so each state must carry the correct parent thread — otherwise
        # both tasks look like they belong to workspace 42.
        their_state = _state("their-task", "running")
        their_state["workspace_thread_id"] = 99
        state_map = {
            "my-task": _state("my-task", "running"),  # workspace_thread_id=42 (fixture default)
            "their-task": their_state,
        }
        ctx = _ctx(entries, state_map)

        # LIST_TASKS sees only mine
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("list_tasks", None, (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "my-task" in body
        assert "their-task" not in body

        # STATUS on their-task from my workspace returns not_found
        subs = asyncio.run(handle_lifecycle_macros(
            [MacroInvocation("task_status", "their-task", (0, 0))], ctx,
        ))
        body = list(subs.values())[0]
        assert "Nessun task attivo" in body


# ─────────────────────────────────────────────────────────────────────────
# Logging invariant
# ─────────────────────────────────────────────────────────────────────────


class TestLogging:
    def test_action_logged_with_resolution(self, caplog, tmp_path, monkeypatch):
        monkeypatch.setattr("continuous.CONTINUOUS_DIR", tmp_path / "continuous")
        import continuous as cont
        cont.save_state(cont.state_file_path("daily-report"), _state("daily-report", "running"))

        queue_file = tmp_path / "queue.json"
        queue_file.write_text("[]")
        monkeypatch.setattr("scheduler.QUEUE_FILE", queue_file)

        entries = [_entry("daily-report", "continuous", status="running")]
        state_map = {"daily-report": _state("daily-report", "running")}
        ctx = _ctx(entries, state_map)
        with caplog.at_level(logging.INFO, logger="robyx.lifecycle_macros"):
            asyncio.run(handle_lifecycle_macros(
                [MacroInvocation("stop_task", "daily-report", (0, 0))], ctx,
            ))
        msgs = [rec.getMessage() for rec in caplog.records
                if rec.name == "robyx.lifecycle_macros"]
        assert any("macro=stop_task" in m and "daily-report" in m and "stopped" in m
                   for m in msgs)


# ─────────────────────────────────────────────────────────────────────────
# Substitute macros — in-place splice
# ─────────────────────────────────────────────────────────────────────────


class TestSubstituteMacros:
    def test_substitute_replaces_spans_in_reverse_order(self):
        text = "prefix [LIST_TASKS] middle [STOP_TASK name=\"x\"] suffix"
        invs = parse_lifecycle_macros(text)
        subs = {invs[0].span: "LIST_BODY", invs[1].span: "STOP_BODY"}
        out = substitute_macros(text, subs)
        assert out == "prefix LIST_BODY middle STOP_BODY suffix"

    def test_substitute_empty_mapping_returns_text_unchanged(self):
        assert substitute_macros("hello", {}) == "hello"

    def test_substitute_collapses_excess_newlines(self):
        text = "a [LIST_TASKS]\n\n\n\nb"
        invs = parse_lifecycle_macros(text)
        out = substitute_macros(text, {invs[0].span: ""})
        assert "\n\n\n" not in out
