"""Tests for bot/task_runtime.py — scheduled task context resolution."""

import json
from pathlib import Path

import pytest


# ── validate_task_name ────────────────────────────────────────────────────


class TestValidateTaskName:
    def test_simple_name(self):
        from task_runtime import validate_task_name

        assert validate_task_name("my-task") == "my-task"

    def test_strips_whitespace(self):
        from task_runtime import validate_task_name

        assert validate_task_name("  hello  ") == "hello"

    def test_rejects_empty(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="required"):
            validate_task_name("")

    def test_rejects_none(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="required"):
            validate_task_name(None)

    def test_rejects_absolute_path(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="single relative path"):
            validate_task_name("/etc/passwd")

    def test_rejects_directory_traversal(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="single relative path"):
            validate_task_name("../sibling")

    def test_rejects_multi_segment(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="single relative path"):
            validate_task_name("a/b")

    @pytest.mark.parametrize("bad_char", ["\n", "\r", "\t", "\0", "|"])
    def test_rejects_control_chars(self, bad_char):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="unsupported"):
            validate_task_name("name" + bad_char + "bad")

    def test_rejects_dot(self):
        from task_runtime import validate_task_name

        with pytest.raises(ValueError, match="single relative path"):
            validate_task_name(".")


# ── validate_agent_file_ref ───────────────────────────────────────────────


class TestValidateAgentFileRef:
    def test_workspace_agent_ref(self):
        from task_runtime import validate_agent_file_ref

        ref, agent_type = validate_agent_file_ref("agents/my-agent.md")
        assert ref == "agents/my-agent.md"
        assert agent_type == "workspace"

    def test_specialist_agent_ref(self):
        from task_runtime import validate_agent_file_ref

        ref, agent_type = validate_agent_file_ref("specialists/reviewer.md")
        assert ref == "specialists/reviewer.md"
        assert agent_type == "specialist"

    def test_rejects_empty(self):
        from task_runtime import validate_agent_file_ref

        with pytest.raises(ValueError, match="required"):
            validate_agent_file_ref("")

    def test_rejects_wrong_directory(self):
        from task_runtime import validate_agent_file_ref

        with pytest.raises(ValueError, match="agents.*specialists"):
            validate_agent_file_ref("data/foo.md")

    def test_rejects_non_md(self):
        from task_runtime import validate_agent_file_ref

        with pytest.raises(ValueError, match="markdown"):
            validate_agent_file_ref("agents/foo.txt")

    def test_rejects_absolute(self):
        from task_runtime import validate_agent_file_ref

        with pytest.raises(ValueError, match="agents.*specialists"):
            validate_agent_file_ref("/agents/foo.md")

    def test_rejects_nested(self):
        from task_runtime import validate_agent_file_ref

        with pytest.raises(ValueError, match="agents.*specialists"):
            validate_agent_file_ref("agents/sub/foo.md")

    def test_normalizes_backslash(self):
        from task_runtime import validate_agent_file_ref

        ref, _ = validate_agent_file_ref("agents\\my-agent.md")
        assert ref == "agents/my-agent.md"


# ── resolve_task_runtime_context ──────────────────────────────────────────


class TestResolveTaskRuntimeContext:
    def test_fallback_to_workspace_when_no_state(self, tmp_path):
        from task_runtime import resolve_task_runtime_context
        import config as cfg

        task = {
            "name": "test-task",
            "agent_file": "agents/test-task.md",
        }
        ctx = resolve_task_runtime_context(task)
        assert ctx.agent_name == "test-task"
        assert ctx.agent_type == "workspace"
        assert ctx.work_dir == str(cfg.WORKSPACE)

    def test_uses_stored_agent_work_dir(self, tmp_path):
        from task_runtime import resolve_task_runtime_context
        import config as cfg

        # Write a state file with a known agent
        state = {
            "agents": {
                "my-agent": {
                    "name": "my-agent",
                    "work_dir": "/custom/path",
                    "agent_type": "workspace",
                    "description": "test",
                    "session_id": "abc",
                    "message_count": 0,
                }
            }
        }
        cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg.STATE_FILE.write_text(json.dumps(state))

        task = {
            "name": "my-agent",
            "agent_file": "agents/my-agent.md",
        }
        ctx = resolve_task_runtime_context(task)
        assert ctx.agent_name == "my-agent"
        assert ctx.work_dir == "/custom/path"

    def test_infers_specialist_type(self, tmp_path):
        from task_runtime import resolve_task_runtime_context

        task = {
            "name": "review-task",
            "agent_file": "specialists/reviewer.md",
        }
        ctx = resolve_task_runtime_context(task)
        assert ctx.agent_type == "specialist"
        assert ctx.agent_name == "reviewer"

    def test_handles_corrupt_state_file(self, tmp_path):
        from task_runtime import resolve_task_runtime_context
        import config as cfg

        cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg.STATE_FILE.write_text("not json")

        task = {
            "name": "test-task",
            "agent_file": "agents/test-task.md",
        }
        ctx = resolve_task_runtime_context(task)
        assert ctx.agent_name == "test-task"
        assert ctx.work_dir == str(cfg.WORKSPACE)
