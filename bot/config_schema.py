"""Typed validation for Robyx environment configuration.

This module is deliberately stdlib-only so the setup wizard can reuse the
same contract as the runtime and chat-update path before optional runtime
dependencies are available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class EnvValueKind(str, Enum):
    STRING = "string"
    ENUM = "enum"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    EXECUTABLE_PATH = "executable_path"
    DIRECTORY_PATH = "directory_path"


class EnvValidationError(ValueError):
    """A value failed its public, non-secret configuration contract."""

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__("Invalid configuration for %s: %s" % (key, reason))


@dataclass(frozen=True)
class EnvField:
    kind: EnvValueKind
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    allow_empty: bool = False
    secret: bool = False

    def validate(self, key: str, raw_value: object) -> str:
        """Validate and normalise one value without including it in errors."""
        if raw_value is None:
            value = ""
        elif isinstance(raw_value, str):
            value = raw_value
        else:
            value = str(raw_value)

        if "\x00" in value or "\n" in value or "\r" in value:
            raise EnvValidationError(key, "must be a single-line value")

        if self.kind is EnvValueKind.STRING:
            if not value and not self.allow_empty:
                raise EnvValidationError(key, "must not be empty")
            return value

        value = value.strip()
        if not value:
            if self.allow_empty:
                return ""
            raise EnvValidationError(key, "must not be empty")

        if self.kind is EnvValueKind.ENUM:
            if value not in self.choices:
                raise EnvValidationError(
                    key,
                    "must be one of %s" % ", ".join(self.choices),
                )
            return value

        if self.kind is EnvValueKind.INTEGER:
            try:
                parsed = int(value, 10)
            except ValueError as exc:
                raise EnvValidationError(key, "must be an integer") from exc
            if self.minimum is not None and parsed < self.minimum:
                raise EnvValidationError(key, "must be at least %d" % self.minimum)
            if self.maximum is not None and parsed > self.maximum:
                raise EnvValidationError(key, "must be at most %d" % self.maximum)
            return str(parsed)

        if self.kind is EnvValueKind.BOOLEAN:
            lowered = value.lower()
            if lowered in {"1", "true", "yes", "on"}:
                return "true"
            if lowered in {"0", "false", "no", "off"}:
                return "false"
            raise EnvValidationError(key, "must be true or false")

        expanded = Path(os.path.expanduser(value))
        if self.kind is EnvValueKind.EXECUTABLE_PATH:
            if not expanded.is_file() or not os.access(expanded, os.X_OK):
                raise EnvValidationError(
                    key,
                    "must point to an existing executable file",
                )
            return value

        if self.kind is EnvValueKind.DIRECTORY_PATH:
            if expanded.exists() and not expanded.is_dir():
                raise EnvValidationError(
                    key,
                    "must point to a directory (or a directory that can be created)",
                )
            return value

        raise EnvValidationError(key, "uses an unsupported value type")


_BACKENDS = ("claude", "codex", "opencode")
_PLATFORMS = ("telegram", "slack", "discord")
_CLAUDE_PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)


# The complete chat-editable surface. Sensitive ownership/platform credentials
# and COLLAB_PARTICIPANT_POLICY intentionally do not appear here.
CHAT_ENV_FIELDS: Mapping[str, EnvField] = {
    "OPENAI_API_KEY": EnvField(
        EnvValueKind.STRING,
        allow_empty=True,
        secret=True,
    ),
    "AI_BACKEND": EnvField(EnvValueKind.ENUM, choices=_BACKENDS),
    "AI_CLI_PATH": EnvField(
        EnvValueKind.EXECUTABLE_PATH,
        allow_empty=True,
    ),
    "CLAUDE_PERMISSION_MODE": EnvField(
        EnvValueKind.ENUM,
        choices=_CLAUDE_PERMISSION_MODES,
        allow_empty=True,
    ),
    "SCHEDULER_INTERVAL": EnvField(
        EnvValueKind.INTEGER,
        minimum=1,
        maximum=86_400,
    ),
    "UPDATE_CHECK_INTERVAL": EnvField(
        EnvValueKind.INTEGER,
        minimum=1,
        maximum=2_592_000,
    ),
    "REMINDER_MAX_AGE_SECONDS": EnvField(
        EnvValueKind.INTEGER,
        minimum=1,
        maximum=31_536_000,
    ),
    "ROBYX_PLATFORM": EnvField(EnvValueKind.ENUM, choices=_PLATFORMS),
    "ROBYX_WORKSPACE": EnvField(EnvValueKind.DIRECTORY_PATH),
    # Legacy aliases retain the exact same contract as their ROBYX names.
    "KAELOPS_PLATFORM": EnvField(EnvValueKind.ENUM, choices=_PLATFORMS),
    "KAELOPS_WORKSPACE": EnvField(EnvValueKind.DIRECTORY_PATH),
}


# Settings that must be changed locally and must never be forwarded to an AI
# backend when a chat message resembles an environment assignment.  The set
# includes credentials, authority identifiers, and permission/sandbox knobs.
# ``OPENAI_API_KEY`` remains deliberately chat-editable in owner HQ, but its
# ``secret=True`` schema marker lets collaborative lanes reject it as well.
LOCAL_ONLY_ENV_KEYS = frozenset({
    "ROBYX_BOT_TOKEN",
    "KAELOPS_BOT_TOKEN",
    "ROBYX_CHAT_ID",
    "KAELOPS_CHAT_ID",
    "ROBYX_OWNER_ID",
    "KAELOPS_OWNER_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "SLACK_OWNER_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_CONTROL_CHANNEL_ID",
    "DISCORD_OWNER_ID",
    "ANTHROPIC_API_KEY",
    "COLLAB_PARTICIPANT_POLICY",
    "CODEX_APPROVAL_POLICY",
    "CODEX_SANDBOX",
    "OPENCODE_CONFIG",
    "OPENCODE_PERMISSION",
    "DISCORD_INVITE_TTL_DAYS",
    "DISCORD_INVITE_MAX_USES",
    "AI_MODEL_DEFAULTS",
    "AI_MODEL_ALIASES",
    "AI_IDLE_TIMEOUT",
    "AI_TIMEOUT",
    "CLAIM_TIMEOUT_SECONDS",
    "LOCK_HEARTBEAT_INTERVAL_SECONDS",
    "LOCK_STALE_THRESHOLD_SECONDS",
    "ORPHAN_INCIDENT_THRESHOLD",
    "EVENT_RETENTION_DAYS",
    "EVENT_MAX_HOT_BYTES",
    "AWAITING_REMINDER_SECONDS",
    "DRAIN_TIMEOUT_DEFAULT_SECONDS",
    "TOPIC_UNREACHABLE_RETRY_WINDOW_SECONDS",
    "SMOKE_TEST_TIMEOUT_SECONDS",
    "VOICE_TIMEOUT_SECONDS",
})

SECRET_CHAT_ENV_KEYS = frozenset(
    key for key, field in CHAT_ENV_FIELDS.items() if field.secret
)


def validate_env_value(key: str, value: object) -> str:
    """Validate one known chat-editable value."""
    try:
        field = CHAT_ENV_FIELDS[key]
    except KeyError as exc:
        raise EnvValidationError(key, "is not a supported setting") from exc
    return field.validate(key, value)


def validate_env_updates(updates: Mapping[str, object]) -> dict[str, str]:
    """Validate a batch atomically and return its normalised representation."""
    return {key: validate_env_value(key, value) for key, value in updates.items()}


def typed_env(
    key: str,
    value: object,
    *,
    as_type: type[str] | type[int] | type[Path] = str,
):
    """Validate a runtime value through the shared schema and convert it."""
    validated = validate_env_value(key, value)
    if as_type is int:
        return int(validated)
    if as_type is Path:
        return Path(validated)
    return validated
