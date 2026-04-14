"""Tests for the ai_backend module — backends + factory."""

import json
import os
from unittest.mock import patch

import pytest

from ai_backend import (
    ClaudeBackend,
    CodexBackend,
    OpenCodeBackend,
    create_backend,
    list_backends,
)


# ═══════════════════════════════════════════════════════════════════
# ClaudeBackend
# ═══════════════════════════════════════════════════════════════════


class TestClaudeBackend:
    """Tests for ClaudeBackend."""

    def test_name(self, claude_backend):
        assert claude_backend.name == "Claude Code"

    def test_supports_sessions(self, claude_backend):
        assert claude_backend.supports_sessions() is True

    def test_supports_streaming(self, claude_backend):
        assert claude_backend.supports_streaming() is True

    # ── build_command ──

    def test_build_command_basic(self, claude_backend):
        cmd = claude_backend.build_command(
            message="hello",
            session_id=None,
            system_prompt=None,
            model="sonnet",
            work_dir="/tmp",
            is_resume=False,
        )
        assert cmd[0] == "/usr/bin/claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        # bypassPermissions is the default for autonomous operation
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert "hello" not in cmd
        assert claude_backend.command_stdin_payload("hello") == b"hello\n"

    def test_build_command_includes_permission_mode_when_configured(self):
        backend = ClaudeBackend("/usr/bin/claude", permission_mode="bypassPermissions")
        cmd = backend.build_command(
            message="hello",
            session_id=None,
            system_prompt=None,
            model="sonnet",
            work_dir="/tmp",
            is_resume=False,
        )
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"

    def test_build_command_with_system_prompt(self, claude_backend):
        cmd = claude_backend.build_command(
            message="hi",
            session_id=None,
            system_prompt="You are helpful.",
            model="sonnet",
            work_dir="/tmp",
            is_resume=False,
        )
        assert "--append-system-prompt" in cmd
        assert cmd[cmd.index("--append-system-prompt") + 1] == "You are helpful."

    def test_build_command_session_id_no_resume(self, claude_backend):
        cmd = claude_backend.build_command(
            message="hi",
            session_id="sess-123",
            system_prompt=None,
            model="sonnet",
            work_dir="/tmp",
            is_resume=False,
        )
        assert "--session-id" in cmd
        assert cmd[cmd.index("--session-id") + 1] == "sess-123"
        assert "--resume" not in cmd

    def test_build_command_session_id_with_resume(self, claude_backend):
        cmd = claude_backend.build_command(
            message="hi",
            session_id="sess-123",
            system_prompt=None,
            model="sonnet",
            work_dir="/tmp",
            is_resume=True,
        )
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-123"
        assert "--session-id" not in cmd

    def test_build_command_no_session_id(self, claude_backend):
        cmd = claude_backend.build_command(
            message="hi",
            session_id=None,
            system_prompt=None,
            model="sonnet",
            work_dir="/tmp",
            is_resume=False,
        )
        assert "--session-id" not in cmd
        assert "--resume" not in cmd

    # ── build_spawn_command ──

    def test_build_spawn_command(self, claude_backend):
        cmd = claude_backend.build_spawn_command(
            prompt="do stuff",
            model="opus",
            work_dir="/home/user/project",
        )
        assert cmd[0] == "/usr/bin/claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "/home/user/project"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"
        # Spawn commands always force bypassPermissions (no terminal)
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        # stream-json should NOT be present
        assert "stream-json" not in cmd
        assert "do stuff" not in cmd
        assert claude_backend.spawn_stdin_payload("do stuff") == b"do stuff\n"

    def test_build_spawn_command_includes_permission_mode_when_configured(self):
        backend = ClaudeBackend("/usr/bin/claude", permission_mode="bypassPermissions")
        cmd = backend.build_spawn_command(
            prompt="do stuff",
            model="opus",
            work_dir="/home/user/project",
        )
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"

    # ── parse_response ──

    def test_parse_response_stream_json(self, claude_backend):
        lines = [
            json.dumps({"type": "assistant", "content": "thinking..."}),
            json.dumps({"type": "result", "result": "Final answer"}),
        ]
        stdout = "\n".join(lines)
        assert claude_backend.parse_response(stdout, 0) == "Final answer"

    def test_parse_response_empty(self, claude_backend):
        assert claude_backend.parse_response("", 0) == ""

    def test_parse_response_single_json(self, claude_backend):
        stdout = json.dumps({"result": "single object"})
        assert claude_backend.parse_response(stdout, 0) == "single object"

    def test_parse_response_non_json(self, claude_backend):
        stdout = "plain text output"
        assert claude_backend.parse_response(stdout, 0) == "plain text output"

    def test_parse_response_stream_json_no_result_event(self, claude_backend):
        """When no line has type=result, fallback to single-JSON parse."""
        lines = [
            json.dumps({"type": "assistant", "content": "step 1"}),
            json.dumps({"type": "assistant", "content": "step 2"}),
        ]
        stdout = "\n".join(lines)
        # Neither line has type=result, and the whole string is not valid
        # single JSON either, so it falls through to raw return.
        result = claude_backend.parse_response(stdout, 0)
        assert result == stdout


# ═══════════════════════════════════════════════════════════════════
# CodexBackend
# ═══════════════════════════════════════════════════════════════════


class TestCodexBackend:
    """Tests for CodexBackend."""

    def test_name(self, codex_backend):
        assert codex_backend.name == "Codex CLI"

    def test_supports_sessions(self, codex_backend):
        assert codex_backend.supports_sessions() is False

    def test_supports_streaming(self, codex_backend):
        assert codex_backend.supports_streaming() is False

    # ── build_command ──

    def test_build_command(self, codex_backend):
        cmd = codex_backend.build_command(
            message="explain this",
            session_id=None,
            system_prompt="Be concise.",
            model="gpt-4",
            work_dir="/tmp",
            is_resume=False,
        )
        assert cmd[0] == "/usr/bin/codex"
        assert "-q" in cmd
        assert cmd[cmd.index("-q") + 1] == "explain this"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-4"
        assert "--system-prompt" in cmd
        assert cmd[cmd.index("--system-prompt") + 1] == "Be concise."

    # ── build_spawn_command ──

    def test_build_spawn_command(self, codex_backend):
        cmd = codex_backend.build_spawn_command(
            prompt="run task",
            model="gpt-4",
            work_dir="/tmp",
        )
        assert "-q" in cmd
        assert cmd[cmd.index("-q") + 1] == "run task"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-4"

    # ── parse_response ──

    def test_parse_response(self, codex_backend):
        assert codex_backend.parse_response("  answer  \n", 0) == "answer"

    def test_parse_response_empty(self, codex_backend):
        assert codex_backend.parse_response("", 0) == ""

    # ── unsafe-autonomy defaults ──

    def test_build_command_defaults_to_unsafe_autonomy(self, codex_backend):
        cmd = codex_backend.build_command(
            message="hi", session_id=None, system_prompt=None,
            model="gpt-4", work_dir="/tmp", is_resume=False,
        )
        assert "--approval-policy" in cmd
        assert cmd[cmd.index("--approval-policy") + 1] == "never"
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"

    def test_build_spawn_command_forces_unsafe_autonomy(self, codex_backend):
        cmd = codex_backend.build_spawn_command(
            prompt="t", model="gpt-4", work_dir="/tmp",
        )
        assert cmd[cmd.index("--approval-policy") + 1] == "never"
        assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"

    def test_env_overrides_approval_policy_and_sandbox(self, monkeypatch):
        from ai_backend import CodexBackend
        monkeypatch.setenv("CODEX_APPROVAL_POLICY", "on-request")
        monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")
        backend = CodexBackend("/usr/bin/codex")
        cmd = backend.build_command(
            message="hi", session_id=None, system_prompt=None,
            model="gpt-4", work_dir="/tmp", is_resume=False,
        )
        assert cmd[cmd.index("--approval-policy") + 1] == "on-request"
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


# ═══════════════════════════════════════════════════════════════════
# OpenCodeBackend
# ═══════════════════════════════════════════════════════════════════


class TestOpenCodeBackend:
    """Tests for OpenCodeBackend."""

    def test_name(self, opencode_backend):
        assert opencode_backend.name == "OpenCode"

    def test_supports_sessions(self, opencode_backend):
        # OpenCode supports sessions through ``--session <id>``; Robyx reuses
        # the id only when it is the backend's native ``ses_…`` form.
        assert opencode_backend.supports_sessions() is True

    def test_can_resume_session_filters_non_native_ids(self, opencode_backend):
        # Robyx stores a UUID per agent for its own bookkeeping. OpenCode
        # would reject it, so the resume guard MUST refuse non-native ids.
        assert opencode_backend.can_resume_session(None) is False
        assert opencode_backend.can_resume_session("") is False
        assert opencode_backend.can_resume_session("00000000-0000-0000-0000-000000000001") is False
        assert opencode_backend.can_resume_session("ses_abc123") is True

    # ── build_command ──

    def test_build_command_emits_format_json_and_message(self, opencode_backend):
        cmd = opencode_backend.build_command(
            message="do something",
            session_id=None,
            system_prompt=None,
            model="deepseek",
            work_dir="/tmp",
            is_resume=False,
        )
        assert cmd[0] == "/usr/bin/opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"
        # No provider-qualified model → --model is omitted entirely.
        assert "--model" not in cmd
        # The message must be the very last argument so OpenCode's CLI parser
        # cannot mistake it for an option value.
        assert cmd[-1] == "do something"

    def test_build_command_with_provider_model(self, opencode_backend):
        cmd = opencode_backend.build_command(
            message="do something",
            session_id=None,
            system_prompt=None,
            model="openai/gpt-5.4",
            work_dir="/tmp",
            is_resume=False,
        )
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.4"
        assert cmd[-1] == "do something"

    def test_build_command_includes_session_when_resuming(self, opencode_backend):
        cmd = opencode_backend.build_command(
            message="next turn",
            session_id="ses_abcdef",
            system_prompt=None,
            model=None,
            work_dir="/tmp",
            is_resume=True,
        )
        assert "--session" in cmd
        assert cmd[cmd.index("--session") + 1] == "ses_abcdef"

    def test_build_command_drops_non_native_session(self, opencode_backend):
        # Robyx' UUID must never reach the CLI — it would be rejected.
        cmd = opencode_backend.build_command(
            message="next turn",
            session_id="00000000-0000-0000-0000-000000000001",
            system_prompt=None,
            model=None,
            work_dir="/tmp",
            is_resume=True,
        )
        assert "--session" not in cmd

    def test_build_command_inlines_system_prompt(self, opencode_backend):
        # OpenCode has no --system-prompt flag, so Robyx wraps the prompt
        # inside the user message between explicit tags.
        cmd = opencode_backend.build_command(
            message="hello",
            session_id=None,
            system_prompt="Be terse.",
            model=None,
            work_dir="/tmp",
            is_resume=False,
        )
        composed = cmd[-1]
        assert "<system_instructions>" in composed
        assert "Be terse." in composed
        assert "<user_message>" in composed
        assert "hello" in composed

    def test_build_spawn_command_format_json(self, opencode_backend):
        cmd = opencode_backend.build_spawn_command(
            prompt="run me", model="openai/gpt-5", work_dir="/tmp",
        )
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"
        assert cmd[-1] == "run me"

    # ── parse_response ──

    def test_parse_response_empty(self, opencode_backend):
        assert opencode_backend.parse_response("", 0) == {"text": "", "session_id": None}

    def test_parse_response_falls_back_to_plain_text(self, opencode_backend):
        # Non-JSON output (e.g. an error or older OpenCode build) is returned
        # as a text-only payload with no session id.
        result = opencode_backend.parse_response("  hello world\n", 0)
        assert result == {"text": "hello world", "session_id": None}

    def test_parse_response_extracts_text_and_session_from_ndjson(self, opencode_backend):
        ndjson = (
            '{"sessionID": "ses_abc"}\n'
            '{"part": {"text": "first chunk"}}\n'
            '{"result": "final answer"}\n'
        )
        result = opencode_backend.parse_response(ndjson, 0)
        assert result["session_id"] == "ses_abc"
        assert result["text"] == "final answer"

    def test_parse_response_extracts_session_from_nested_object(self, opencode_backend):
        payload = (
            '{"message": {"content": [{"text": "hi"}, {"text": "there"}]}, '
            '"session": {"sessionId": "ses_xyz"}}'
        )
        result = opencode_backend.parse_response(payload, 0)
        assert result["session_id"] == "ses_xyz"
        assert "hi" in result["text"]
        assert "there" in result["text"]


    # ── managed-config for unsafe-autonomy defaults ──

    def test_managed_config_written_with_allow_permission(self, tmp_path, monkeypatch):
        """Instantiating OpenCodeBackend must write a managed config with
        ``permission: allow`` and export OPENCODE_CONFIG so the CLI picks it up."""
        import config as cfg
        from ai_backend import OpenCodeBackend

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
        monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
        monkeypatch.delenv("OPENCODE_PERMISSION", raising=False)

        backend = OpenCodeBackend("/usr/bin/opencode")
        expected = tmp_path / "opencode-managed.json"
        assert os.environ.get("OPENCODE_CONFIG") == str(expected)
        assert expected.exists()
        data = json.loads(expected.read_text())
        assert data["permission"] == "allow"
        assert backend.permission == "allow"

    def test_existing_opencode_config_is_respected(self, tmp_path, monkeypatch):
        """If the user has already exported OPENCODE_CONFIG, we must not
        overwrite their choice — they've opted out of Robyx' defaults."""
        import config as cfg
        from ai_backend import OpenCodeBackend

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
        monkeypatch.setenv("OPENCODE_CONFIG", "/user/custom.json")
        OpenCodeBackend("/usr/bin/opencode")
        assert os.environ["OPENCODE_CONFIG"] == "/user/custom.json"
        assert not (tmp_path / "opencode-managed.json").exists()

    def test_opencode_permission_env_overrides_default(self, tmp_path, monkeypatch):
        import config as cfg
        from ai_backend import OpenCodeBackend

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
        monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
        monkeypatch.setenv("OPENCODE_PERMISSION", "ask")
        OpenCodeBackend("/usr/bin/opencode")
        data = json.loads((tmp_path / "opencode-managed.json").read_text())
        assert data["permission"] == "ask"


# ═══════════════════════════════════════════════════════════════════
# Factory — create_backend / list_backends
# ═══════════════════════════════════════════════════════════════════


class TestFactory:
    """Tests for create_backend and list_backends."""

    def test_create_claude(self):
        b = create_backend("claude", "/path/to/claude")
        assert isinstance(b, ClaudeBackend)
        assert b.cli_path == "/path/to/claude"

    def test_create_codex(self):
        b = create_backend("codex", "/path/to/codex")
        assert isinstance(b, CodexBackend)
        assert b.cli_path == "/path/to/codex"

    def test_create_opencode(self):
        b = create_backend("opencode", "/path/to/opencode")
        assert isinstance(b, OpenCodeBackend)
        assert b.cli_path == "/path/to/opencode"

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            create_backend("unknown")

    def test_create_auto_detect_path(self):
        with patch("ai_backend.shutil.which", return_value="/usr/local/bin/claude"):
            b = create_backend("claude")
            assert isinstance(b, ClaudeBackend)
            assert b.cli_path == "/usr/local/bin/claude"

    def test_create_auto_detect_not_found(self):
        with patch("ai_backend.shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="not found on PATH"):
                create_backend("claude")

    def test_list_backends(self):
        result = list_backends()
        assert result == ["claude", "codex", "opencode"]
