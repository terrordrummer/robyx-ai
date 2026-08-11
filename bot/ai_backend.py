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

from execution_policy import (
    BackendInvocation,
    InvocationSecurityContext,
    SYSTEM_INVOCATION,
    UnsupportedExecutionProfile,
)

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
        security_context: InvocationSecurityContext = SYSTEM_INVOCATION,
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

    def build_invocation(
        self,
        *,
        message: str,
        session_id: str | None,
        system_prompt: str | None,
        model: str,
        work_dir: str,
        is_resume: bool,
        security_context: InvocationSecurityContext,
    ) -> BackendInvocation:
        """Build a command and its child-only execution environment.

        Backends override :meth:`child_env_overrides` when a security profile
        needs an isolated config file.  Participant turns are deliberately
        stateless even when the underlying CLI supports sessions.
        """
        return BackendInvocation(
            argv=self.build_command(
                message=message,
                session_id=session_id,
                system_prompt=system_prompt,
                model=model,
                work_dir=work_dir,
                is_resume=is_resume,
                security_context=security_context,
            ),
            env_overrides=self.child_env_overrides(security_context),
            persist_session=not security_context.is_participant,
        )

    def child_env_overrides(
        self,
        security_context: InvocationSecurityContext,
    ) -> dict[str, str]:
        """Environment overrides applied only to the spawned CLI process."""
        return {}

    def participant_probe_commands(self) -> list[list[str]]:
        """Return offline commands used to verify participant capabilities.

        A backend that does not override this method cannot be used for a
        participant turn.  Executive and system turns are unaffected.
        """
        raise UnsupportedExecutionProfile(
            "%s does not declare participant capability probes" % self.name
        )

    def validate_participant_probe(self, outputs: list[str]) -> str | None:
        """Return an error string when offline capability probe output is unsafe."""
        raise UnsupportedExecutionProfile(
            "%s does not validate participant capabilities" % self.name
        )

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
            security_context=SYSTEM_INVOCATION,
        )

    def build_spawn_invocation(
        self,
        *,
        prompt: str,
        model: str,
        work_dir: str,
    ) -> BackendInvocation:
        """Build a scheduled command together with its child-only env."""
        return BackendInvocation(
            argv=self.build_spawn_command(prompt, model, work_dir),
            env_overrides=self.child_env_overrides(SYSTEM_INVOCATION),
            persist_session=False,
        )


class ClaudeBackend(AIBackend):
    """Claude Code CLI backend."""

    def __init__(self, cli_path: str, permission_mode: str | None = None):
        super().__init__(cli_path)
        # Default to bypassPermissions so agents can operate autonomously.
        # Override via CLAUDE_PERMISSION_MODE env var or constructor arg.
        self.permission_mode = (
            permission_mode
            if permission_mode is not None
            else os.environ.get("CLAUDE_PERMISSION_MODE", "").strip()
            or "bypassPermissions"
        )

    @property
    def name(self) -> str:
        return "Claude Code"

    def supports_sessions(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def build_command(
        self, message, session_id, system_prompt, model, work_dir, is_resume,
        security_context=SYSTEM_INVOCATION,
    ):
        cmd = [
            self.cli_path,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
        ]
        if security_context.is_participant:
            # ``--tools`` is an availability allowlist, not a prompt hint.
            # Safe mode removes project/user hooks, plugins, skills and MCP;
            # strict empty MCP config is belt-and-braces for older settings.
            cmd.extend([
                "--permission-mode", "plan",
                "--tools", "Read,Glob,Grep",
                "--safe-mode",
                "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}',
                "--no-session-persistence",
            ])
        elif self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        if session_id and not security_context.is_participant:
            if is_resume:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        return cmd

    def participant_probe_commands(self) -> list[list[str]]:
        return [[self.cli_path, "--help"]]

    def validate_participant_probe(self, outputs: list[str]) -> str | None:
        help_text = outputs[0] if outputs else ""
        required = (
            "--tools", "--safe-mode", "--strict-mcp-config",
            "--no-session-persistence", "plan",
        )
        missing = [token for token in required if token not in help_text]
        if missing:
            return "Claude CLI is missing required flags: %s" % ", ".join(missing)
        return None

    def build_spawn_command(self, prompt, model, work_dir):
        cmd = [
            self.cli_path,
            "-p",
            "--model", model,
            "--output-format", "json",
            "-d", work_dir,
        ]
        # Spawned tasks run without a terminal — force permission bypass
        # so they never block waiting for interactive approval.
        cmd.extend(["--permission-mode", "bypassPermissions"])
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
    """OpenAI Codex CLI backend.

    Targets ``codex exec`` (non-interactive subcommand) on Codex CLI 0.124+,
    where the legacy top-level ``-q``/``--approval-policy``/``--system-prompt``
    flags no longer exist. Defaults to unsafe autonomous execution
    (``approval_policy=never`` + ``--sandbox danger-full-access``) so spawned
    agents can modify the workspace without human prompts. Override
    per-deployment via ``CODEX_APPROVAL_POLICY`` / ``CODEX_SANDBOX`` env vars
    when a stricter policy is explicitly required.
    """

    DEFAULT_APPROVAL_POLICY = "never"
    DEFAULT_SANDBOX = "danger-full-access"
    # Every capability that can create side effects, reach an integration,
    # install dependencies, delegate work or request broader permissions is
    # switched off at CLI precedence. Keep removed/experimental names too:
    # their reappearance must not silently widen an older deployment.
    PARTICIPANT_DISABLED_FEATURES = (
        "apply_patch_freeform",
        "apply_patch_streaming_events",
        "apps",
        "apps_mcp_path_override",
        "artifact",
        "auth_elicitation",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "code_mode",
        "code_mode_buffered_exec",
        "code_mode_host",
        "code_mode_only",
        "codex_git_commit",
        "collaboration_modes",
        "computer_use",
        "deferred_executor",
        "deferred_tool_world_state",
        "enable_fanout",
        "enable_mcp_apps",
        "exec_permission_approvals",
        "external_agent_memory_import",
        "goals",
        "hooks",
        "image_generation",
        "in_app_browser",
        "in_app_updates",
        "js_repl",
        "js_repl_tools_only",
        "mcp_2026_07_28",
        "memories",
        "multi_agent",
        "multi_agent_mode",
        "multi_agent_v2",
        "network_proxy",
        "non_prefixed_mcp_tool_names",
        "plugin_hooks",
        "plugin_sharing",
        "plugins",
        "recommended_plugins",
        "realtime_conversation",
        "remote_control",
        "remote_plugin",
        "request_permissions_tool",
        "request_rule",
        "respect_system_proxy",
        "responses_websockets",
        "responses_websockets_v2",
        "search_tool",
        "shell_snapshot",
        "skill_env_var_dependency_prompt",
        "skill_mcp_dependency_install",
        "skill_search",
        "standalone_web_search",
        "tool_call_mcp_elicitation",
        "tool_search",
        "tool_search_always_defer_mcp_tools",
        "tool_suggest",
        "web_search_cached",
        "web_search_request",
        "workspace_dependencies",
    )
    # Enabled features outside these two explicitly reviewed sets make the
    # local CLI unsupported for participant turns. This turns future default-
    # on features into a refusal instead of an implicit capability grant.
    PARTICIPANT_ALLOWED_ENABLED_FEATURES = frozenset({
        "enable_request_compression",
        "fast_mode",
        "guardian_approval",
        "item_ids",
        "mentions_v2",
        "personality",
        "remote_compaction_v2",
        "resize_all_images",
        "shell_tool",
        "sqlite",
        "steer",
        "terminal_resize_reflow",
        "tui_app_server",
        "unified_exec",
        "view_image",
    })

    def __init__(
        self,
        cli_path: str,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ):
        super().__init__(cli_path)
        self.approval_policy = (
            approval_policy
            if approval_policy is not None
            else os.environ.get("CODEX_APPROVAL_POLICY", "").strip()
            or self.DEFAULT_APPROVAL_POLICY
        )
        self.sandbox = (
            sandbox
            if sandbox is not None
            else os.environ.get("CODEX_SANDBOX", "").strip()
            or self.DEFAULT_SANDBOX
        )

    @property
    def name(self) -> str:
        return "Codex CLI"

    def supports_sessions(self) -> bool:
        return False

    def _autonomy_flags(self) -> list[str]:
        # Codex CLI 0.124+ removed --approval-policy in favour of TOML
        # config overrides (-c approval_policy=...). --sandbox still exists.
        flags: list[str] = []
        if self.approval_policy:
            flags.extend(["-c", 'approval_policy="%s"' % self.approval_policy])
        if self.sandbox:
            flags.extend(["--sandbox", self.sandbox])
        return flags

    @staticmethod
    def _compose_message(message: str, system_prompt: str | None) -> str:
        """Inline the system prompt into the user message.

        Codex `exec` does not accept a separate system-prompt flag, so the
        orchestrator's system instructions are wrapped in tagged sections
        inside the user message — same pattern used for OpenCode.
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

    def build_command(
        self, message, session_id, system_prompt, model, work_dir, is_resume,
        security_context=SYSTEM_INVOCATION,
    ):
        cmd = [self.cli_path, "exec", "--skip-git-repo-check"]
        if security_context.is_participant:
            # Highest-precedence CLI overrides: ignore user-supplied MCPs and
            # rules, prohibit escalation, disable network-capable/integration
            # features, and expose an empty environment to model-run commands.
            cmd.extend([
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--ephemeral",
                "-c", 'approval_policy="never"',
                "--sandbox", "read-only",
                "-c", 'web_search="disabled"',
                "-c", "mcp_servers={}",
                "-c", 'shell_environment_policy.inherit="none"',
                "-c", "shell_environment_policy.ignore_default_excludes=false",
                "-c", 'shell_environment_policy.set={ PATH = "/usr/bin:/bin" }',
            ])
            for feature in self.PARTICIPANT_DISABLED_FEATURES:
                cmd.extend(["--disable", feature])
        else:
            cmd.extend(self._autonomy_flags())
        if model:
            cmd.extend(["--model", model])
        if work_dir:
            cmd.extend(["--cd", work_dir])
        # `--` terminates option parsing so a prompt that happens to start
        # with a dash isn't misread as a flag.
        cmd.append("--")
        cmd.append(self._compose_message(message, system_prompt))
        return cmd

    def participant_probe_commands(self) -> list[list[str]]:
        return [
            [self.cli_path, "exec", "--help"],
            [self.cli_path, "features", "list"],
        ]

    def validate_participant_probe(self, outputs: list[str]) -> str | None:
        help_text = outputs[0] if outputs else ""
        feature_text = outputs[1] if len(outputs) > 1 else ""
        required_flags = (
            "--sandbox", "read-only", "--ignore-user-config",
            "--ignore-rules", "--ephemeral", "--disable",
        )
        missing_flags = [token for token in required_flags if token not in help_text]
        if missing_flags:
            return "Codex CLI is missing required flags: %s" % ", ".join(missing_flags)
        available_features: set[str] = set()
        enabled_features: set[str] = set()
        malformed_lines: list[str] = []
        for line in feature_text.splitlines():
            fields = line.split()
            if not fields:
                continue
            if len(fields) < 3 or fields[-1] not in ("true", "false"):
                malformed_lines.append(line.strip())
                continue
            name = fields[0]
            available_features.add(name)
            if fields[-1] == "true":
                enabled_features.add(name)
        if malformed_lines:
            return "Codex returned an unrecognized feature inventory"
        missing_features = sorted(
            set(self.PARTICIPANT_DISABLED_FEATURES) - available_features
        )
        if missing_features:
            return "Codex CLI is missing required feature gates: %s" % ", ".join(
                missing_features
            )
        classified_enabled = (
            set(self.PARTICIPANT_DISABLED_FEATURES)
            | set(self.PARTICIPANT_ALLOWED_ENABLED_FEATURES)
        )
        unclassified_enabled = sorted(enabled_features - classified_enabled)
        if unclassified_enabled:
            return (
                "Codex has unclassified enabled features: %s"
                % ", ".join(unclassified_enabled)
            )
        return None

    def build_spawn_command(self, prompt, model, work_dir):
        cmd = [self.cli_path, "exec", "--skip-git-repo-check"]
        # Spawned tasks run without a terminal — always force full autonomy
        # so they never block on an approval prompt nobody can answer.
        cmd.extend(["-c", 'approval_policy="never"', "--sandbox", "danger-full-access"])
        if model:
            cmd.extend(["--model", model])
        if work_dir:
            cmd.extend(["--cd", work_dir])
        cmd.append("--")
        cmd.append(prompt)
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

    **Permissions.** OpenCode has no CLI flag to disable its
    permission-prompting tools (``edit``, ``bash``, ``webfetch``), only a
    JSON config file. For autonomous privileged operation Robyx lazily writes a
    managed config with ``"permission": "allow"`` and passes it to each
    child via ``OPENCODE_CONFIG`` (unless the user already set that variable,
    in which case we defer to their config). No invocation mutates the parent
    environment. Override the privileged default with
    ``OPENCODE_PERMISSION`` (``allow`` | ``ask`` | ``deny``).
    """

    SESSION_PREFIX = "ses_"
    DEFAULT_PERMISSION = "allow"

    @staticmethod
    def _participant_permissions() -> dict[str, str]:
        return {
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "external_directory": "deny",
        }

    @classmethod
    def _participant_config_payload(cls) -> dict[str, Any]:
        permissions = cls._participant_permissions()
        return {
            "$schema": "https://opencode.ai/config.json",
            "permission": permissions,
            "plugin": [],
            "mcp": {},
            "agent": {
                "robyx-participant": {
                    "description": "Robyx read-only collaborative participant",
                    "permission": permissions,
                },
            },
        }

    def __init__(self, cli_path: str, permission: str | None = None):
        super().__init__(cli_path)
        self.permission = (
            permission
            if permission is not None
            else os.environ.get("OPENCODE_PERMISSION", "").strip()
            or self.DEFAULT_PERMISSION
        )
        self._configured_path = os.environ.get("OPENCODE_CONFIG", "").strip() or None
        self._executive_config_path: str | None = self._configured_path
        self._participant_config_path: str | None = None
        self._participant_config_dir_path: str | None = None

    def _ensure_managed_config(self) -> None:
        """Write the executive config for child-local use.

        Respects a pre-existing ``OPENCODE_CONFIG`` env var: if the user
        has already configured OpenCode explicitly, we don't override their
        choice for executive/system turns. The generated path is returned via
        :meth:`child_env_overrides`, never exported into the parent process.
        """
        if self._configured_path:
            log.debug(
                "OPENCODE_CONFIG already set, not writing managed config",
            )
            return

        if self._executive_config_path:
            return

        # Import here to avoid a hard dependency cycle with config.py at
        # module import time (ai_backend is imported very early).
        try:
            from config import DATA_DIR  # type: ignore[import-not-found]
        except Exception:
            log.warning(
                "Could not import config.DATA_DIR; skipping managed OpenCode config",
            )
            return

        cfg_path = DATA_DIR / "opencode-managed.json"
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "$schema": "https://opencode.ai/config.json",
                "permission": self.permission,
            }
            tmp_path = cfg_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, cfg_path)
            self._executive_config_path = str(cfg_path)
            log.info(
                "OpenCode managed config written at %s (permission=%s)",
                cfg_path, self.permission,
            )
        except OSError as e:
            log.warning("Failed to write OpenCode managed config: %s", e)

    def _ensure_participant_config(self) -> str:
        """Return a child-only deny-by-default OpenCode config path."""
        if self._participant_config_path:
            return self._participant_config_path
        try:
            from config import DATA_DIR  # type: ignore[import-not-found]
        except Exception as exc:
            raise UnsupportedExecutionProfile(
                "OpenCode participant config directory is unavailable"
            ) from exc

        # OpenCode permission resolution is deny-by-default here.  Only
        # built-in code-reading/navigation tools are re-enabled; shell, edit,
        # network, MCP and subagent/custom-tool actions remain denied.
        payload = self._participant_config_payload()
        cfg_path = DATA_DIR / "opencode-participant.json"
        isolated_config_dir = DATA_DIR / "opencode-participant-config"
        tmp_path = cfg_path.with_suffix(".json.tmp")
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            isolated_config_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(isolated_config_dir, 0o700)
            except OSError:
                pass
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, cfg_path)
        except OSError as exc:
            raise UnsupportedExecutionProfile(
                "Could not create the OpenCode participant config: %s" % exc
            ) from exc
        self._participant_config_path = str(cfg_path)
        self._participant_config_dir_path = str(isolated_config_dir)
        return self._participant_config_path

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

    def build_command(
        self, message, session_id, system_prompt, model, work_dir, is_resume,
        security_context=SYSTEM_INVOCATION,
    ):
        if not security_context.is_participant:
            self._ensure_managed_config()
        cmd = [self.cli_path, "run", "--format", "json"]
        if security_context.is_participant:
            cmd.extend(["--pure", "--agent", "robyx-participant"])
        if (
            session_id and is_resume and self.can_resume_session(session_id)
            and not security_context.is_participant
        ):
            cmd.extend(["--session", session_id])
        if self._supports_explicit_model(model):
            cmd.extend(["--model", model])
        cmd.append(self._compose_message(message, system_prompt))
        return cmd

    def child_env_overrides(
        self,
        security_context: InvocationSecurityContext,
    ) -> dict[str, str]:
        if security_context.is_participant:
            config_path = self._ensure_participant_config()
            # OpenCode supports multiple configuration/plugin/skill env
            # sources. Override every relevant source in the participant
            # child so a permissive parent environment cannot be inherited.
            return {
                "OPENCODE_CONFIG": config_path,
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    self._participant_config_payload(), separators=(",", ":"),
                ),
                "OPENCODE_CONFIG_DIR": self._participant_config_dir_path or "",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "true",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS": "false",
                "OPENCODE_AUTO_SHARE": "false",
                "OPENCODE_PERMISSION": "deny",
                "OPENCODE_PLUGIN_META_FILE": "",
            }
        self._ensure_managed_config()
        if self._executive_config_path:
            return {"OPENCODE_CONFIG": self._executive_config_path}
        return {}

    def participant_probe_commands(self) -> list[list[str]]:
        return [
            [self.cli_path, "--version"],
            [self.cli_path, "run", "--help"],
            [self.cli_path, "debug", "config", "--pure"],
        ]

    def validate_participant_probe(self, outputs: list[str]) -> str | None:
        version_text = (outputs[0] if outputs else "").strip().splitlines()
        version = version_text[0].strip() if version_text else ""
        try:
            parts = tuple(int(p) for p in version.split(".")[:3])
        except ValueError:
            return "Could not parse OpenCode version %r" % version
        if parts < (1, 1, 1):
            return "OpenCode %s is older than the required 1.1.1" % version
        help_text = outputs[1] if len(outputs) > 1 else ""
        missing = [token for token in ("--pure", "--agent") if token not in help_text]
        if missing:
            return "OpenCode is missing required flags: %s" % ", ".join(missing)
        try:
            resolved = json.loads(outputs[2] if len(outputs) > 2 else "")
        except (json.JSONDecodeError, TypeError):
            return "OpenCode did not return a valid resolved participant config"
        expected_permissions = self._participant_permissions()
        participant_agent = (resolved.get("agent") or {}).get("robyx-participant")
        if (
            resolved.get("permission") != expected_permissions
            or resolved.get("plugin") != []
            or resolved.get("mcp") != {}
            or not isinstance(participant_agent, dict)
            or participant_agent.get("permission") != expected_permissions
        ):
            return "OpenCode resolved participant config is not deny-by-default"
        return None

    def build_spawn_command(self, prompt, model, work_dir):
        self._ensure_managed_config()
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


# Per-agent backend selection (workspaces / specialists / scheduled tasks may
# pin themselves to a non-default backend) calls the factory at invocation
# time, so we cache instances to avoid re-running per-backend init side
# effects on every turn — most importantly OpenCode's managed-config write.
_BACKEND_INSTANCE_CACHE: dict[tuple[str, str], AIBackend] = {}


def get_or_create_backend(backend_name: str, cli_path: str | None = None) -> AIBackend:
    """Return a cached backend instance, creating it on first use.

    Intended for the per-agent backend override path: when many turns may
    flow through ``invoke_ai`` for an agent whose ``backend`` differs from
    the global default, we don't want to pay the construction cost (and,
    for OpenCode, the on-disk config write) on every call.

    Tests that mutate environment between calls can clear the cache via
    :func:`reset_backend_cache`.
    """
    key = (backend_name, cli_path or "")
    cached = _BACKEND_INSTANCE_CACHE.get(key)
    if cached is not None:
        return cached
    instance = create_backend(backend_name, cli_path)
    _BACKEND_INSTANCE_CACHE[key] = instance
    return instance


def reset_backend_cache() -> None:
    """Clear the per-agent backend instance cache. Test-only helper."""
    _BACKEND_INSTANCE_CACHE.clear()
