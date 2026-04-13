"""Robyx — AI backend abstraction layer.

Supports multiple CLI-based AI tools through a common interface.
Each backend knows how to build CLI commands, parse responses, and handle sessions.
"""

import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("robyx.backend")


class AIBackend(ABC):
    """Interface for CLI-based AI coding tools."""

    def __init__(self, cli_path: str):
        self.cli_path = cli_path

    @abstractmethod
    def build_command(
        self,
        message: str,
        session_id: str | None,
        system_prompt: str | None,
        model: str,
        work_dir: str,
        is_resume: bool,
    ) -> list[str]:
        """Return the CLI command as a list of strings."""

    @abstractmethod
    def parse_response(self, stdout: str, returncode: int) -> "str | dict[str, Any]":
        """Extract the response payload from CLI output.

        Most backends return a plain text string. Backends that expose extra
        metadata (e.g. a native session ID that must be reused on the next
        turn) may return a ``dict`` containing at least a ``text`` key and
        optionally a ``session_id`` key.
        """

    @abstractmethod
    def supports_sessions(self) -> bool:
        """Whether this backend supports session persistence."""

    def can_resume_session(self, session_id: str | None) -> bool:
        """Whether *session_id* is a valid id this backend can reuse.

        Robyx stores a UUID per agent for its own bookkeeping, but some
        backends (notably OpenCode) only accept their own native session id
        format.  Backends override this to filter out Robyx-only ids.
        """
        return bool(session_id)

    def supports_streaming(self) -> bool:
        """Whether this backend outputs stream-json for line-by-line reading."""
        return False

    def command_stdin_payload(self, message: str) -> bytes | None:
        """Return stdin bytes for the interactive command, if used."""
        return None

    def spawn_stdin_payload(self, prompt: str) -> bytes | None:
        """Return stdin bytes for spawned scheduled runs, if used."""
        return None

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name."""

    def build_spawn_command(
        self,
        prompt: str,
        model: str,
        work_dir: str,
    ) -> list[str]:
        """Build a command for spawning a detached sub-agent (scheduler use).
        Default: same as build_command without session support."""
        return self.build_command(
            message=prompt,
            session_id=None,
            system_prompt=None,
            model=model,
            work_dir=work_dir,
            is_resume=False,
        )


class ClaudeBackend(AIBackend):
    """Claude Code CLI backend."""

    def __init__(self, cli_path: str, permission_mode: str | None = None):
        super().__init__(cli_path)
        self.permission_mode = (
            permission_mode
            if permission_mode is not None
            else os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
        )

    @property
    def name(self) -> str:
        return "Claude Code"

    def supports_sessions(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def build_command(self, message, session_id, system_prompt, model, work_dir, is_resume):
        cmd = [
            self.cli_path,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
        ]
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        if session_id:
            if is_resume:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        return cmd

    def build_spawn_command(self, prompt, model, work_dir):
        cmd = [
            self.cli_path,
            "-p",
            "--model", model,
            "--output-format", "json",
            "-d", work_dir,
        ]
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        return cmd

    @staticmethod
    def _stdin_payload(text: str) -> bytes:
        if text.endswith("\n"):
            return text.encode("utf-8")
        return (text + "\n").encode("utf-8")

    def command_stdin_payload(self, message: str) -> bytes | None:
        return self._stdin_payload(message)

    def spawn_stdin_payload(self, prompt: str) -> bytes | None:
        return self._stdin_payload(prompt)

    def parse_response(self, stdout, returncode):
        if not stdout:
            return ""
        # Handle stream-json: multiple JSON lines, result is in the last "result" event
        for line in reversed(stdout.strip().split('\n')):
            try:
                event = json.loads(line)
                if event.get("type") == "result":
                    return event.get("result", "") or ""
            except json.JSONDecodeError:
                continue
        # Fallback: try as single JSON object
        try:
            result = json.loads(stdout)
            return result.get("result", "") or ""
        except json.JSONDecodeError:
            log.debug("Could not parse Claude response as JSON; returning raw stdout")
            return stdout


class CodexBackend(AIBackend):
    """OpenAI Codex CLI backend."""

    @property
    def name(self) -> str:
        return "Codex CLI"

    def supports_sessions(self) -> bool:
        return False

    def build_command(self, message, session_id, system_prompt, model, work_dir, is_resume):
        cmd = [self.cli_path, "-q", message]
        if model:
            cmd.extend(["--model", model])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        return cmd

    def build_spawn_command(self, prompt, model, work_dir):
        cmd = [self.cli_path, "-q", prompt]
        if model:
            cmd.extend(["--model", model])
        return cmd

    def parse_response(self, stdout, returncode):
        return stdout.strip() if stdout else ""


class OpenCodeBackend(AIBackend):
    """OpenCode CLI backend.

    OpenCode exposes its own session model through ``--session <id>``. The
    CLI emits the chosen session ID in its JSON output (``--format json``);
    Robyx captures it on the first turn and replays it on subsequent turns
    so the conversation stays coherent across messages and bot restarts.

    Native OpenCode session IDs always start with ``ses_`` — Robyx' generic
    UUID is rejected by the CLI, so :meth:`can_resume_session` filters those
    out before they ever reach the command line.
    """

    SESSION_PREFIX = "ses_"

    @staticmethod
    def _supports_explicit_model(model: str | None) -> bool:
        return bool(model and "/" in model)

    @property
    def name(self) -> str:
        return "OpenCode"

    def supports_sessions(self) -> bool:
        return True

    def can_resume_session(self, session_id: str | None) -> bool:
        return bool(session_id and session_id.startswith(self.SESSION_PREFIX))

    @staticmethod
    def _compose_message(message: str, system_prompt: str | None) -> str:
        """Inline the system prompt into the user message.

        OpenCode does not accept a separate ``--system-prompt`` flag, so we
        wrap the orchestrator's system instructions in tagged sections inside
        the user message and instruct the model to honour them.
        """
        if not system_prompt:
            return message
        return (
            "Follow these system instructions exactly. They override any "
            "conflicting defaults.\n\n"
            "<system_instructions>\n"
            "%s\n"
            "</system_instructions>\n\n"
            "<user_message>\n"
            "%s\n"
            "</user_message>"
        ) % (system_prompt, message)

    @staticmethod
    def _extract_session_id(payload: Any) -> str | None:
        """Recursively look for an OpenCode session ID inside a parsed JSON payload."""
        if isinstance(payload, dict):
            for key in ("sessionID", "sessionId", "session_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            session = payload.get("session")
            if isinstance(session, str) and session:
                return session
            if isinstance(session, dict):
                nested = OpenCodeBackend._extract_session_id(session)
                if nested:
                    return nested
            for value in payload.values():
                nested = OpenCodeBackend._extract_session_id(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = OpenCodeBackend._extract_session_id(item)
                if nested:
                    return nested
        return None

    @staticmethod
    def _extract_text(payload: Any) -> str:
        """Extract the assistant text from a single OpenCode JSON event."""
        if not isinstance(payload, dict):
            return ""
        if isinstance(payload.get("result"), str):
            return payload.get("result", "") or ""
        if isinstance(payload.get("text"), str):
            return payload.get("text", "") or ""
        part = payload.get("part")
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                return part.get("text", "") or ""
            if isinstance(part.get("result"), str):
                return part.get("result", "") or ""
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                if parts:
                    return "\n".join(parts).strip()
        return ""

    def build_command(self, message, session_id, system_prompt, model, work_dir, is_resume):
        cmd = [self.cli_path, "run", "--format", "json"]
        if session_id and is_resume and self.can_resume_session(session_id):
            cmd.extend(["--session", session_id])
        if self._supports_explicit_model(model):
            cmd.extend(["--model", model])
        cmd.append(self._compose_message(message, system_prompt))
        return cmd

    def build_spawn_command(self, prompt, model, work_dir):
        cmd = [self.cli_path, "run", "--format", "json"]
        if self._supports_explicit_model(model):
            cmd.extend(["--model", model])
        cmd.append(prompt)
        return cmd

    def parse_response(self, stdout, returncode):
        if not stdout:
            return {"text": "", "session_id": None}

        text = ""
        session_id: str | None = None
        parsed_any = False

        # Try NDJSON first (one event per line — what `--format json` emits
        # when streaming).
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed_any = True
            session_id = session_id or self._extract_session_id(event)
            extracted = self._extract_text(event)
            if extracted:
                text = extracted

        # Fall back to single JSON object (some OpenCode versions emit one
        # blob instead of NDJSON).
        if not parsed_any:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                return {"text": stdout.strip(), "session_id": None}
            session_id = self._extract_session_id(payload)
            text = self._extract_text(payload)

        if not text and not parsed_any:
            text = stdout.strip()

        return {"text": text.strip(), "session_id": session_id}


# ── Factory ──

_BACKENDS = {
    "claude": ClaudeBackend,
    "codex": CodexBackend,
    "opencode": OpenCodeBackend,
}


def create_backend(backend_name: str, cli_path: str | None = None) -> AIBackend:
    """Create an AI backend by name. Auto-detects CLI path if not provided."""
    cls = _BACKENDS.get(backend_name)
    if not cls:
        raise ValueError(
            "Unknown backend: '%s'. Supported: %s" % (backend_name, list(_BACKENDS.keys()))
        )
    if not cli_path:
        # Try to find the CLI on PATH
        default_names = {"claude": "claude", "codex": "codex", "opencode": "opencode"}
        cli_path = shutil.which(default_names.get(backend_name, backend_name))
        if not cli_path:
            raise FileNotFoundError(
                "CLI tool '%s' not found on PATH. Install it or set AI_CLI_PATH in .env"
                % backend_name
            )
    return cls(cli_path)


def list_backends() -> list[str]:
    """Return list of supported backend names."""
    return list(_BACKENDS.keys())
