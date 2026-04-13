"""Helpers for deterministic `.env` updates from chat messages."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Keep this aligned with the documented configuration surface.
KNOWN_ENV_KEYS = frozenset({
    "OPENAI_API_KEY",
    "AI_BACKEND",
    "AI_CLI_PATH",
    "CLAUDE_PERMISSION_MODE",
    "SCHEDULER_INTERVAL",
    "TIMED_SCHEDULER_INTERVAL",
    "UPDATE_CHECK_INTERVAL",
    "ROBYX_PLATFORM",
    "ROBYX_WORKSPACE",
    "ROBYX_BOT_TOKEN",
    "ROBYX_CHAT_ID",
    "ROBYX_OWNER_ID",
    # Legacy names (kael-ops → robyx migration fallback)
    "KAELOPS_PLATFORM",
    "KAELOPS_WORKSPACE",
    "KAELOPS_BOT_TOKEN",
    "KAELOPS_CHAT_ID",
    "KAELOPS_OWNER_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "SLACK_OWNER_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_CONTROL_CHANNEL_ID",
    "DISCORD_OWNER_ID",
})

_DIRECT_ENV_LINE = re.compile(r"^\s*(?:set\s+)?([A-Z0-9_]+)\s*[:=]\s*(.+?)\s*$")


def parse_direct_env_updates(text: str | None) -> dict[str, str]:
    """Parse strict ``KEY=value`` / ``KEY: value`` chat messages.

    Returns an empty dict unless every non-empty line is a known env-key
    assignment. This keeps normal natural-language messages on the AI path
    while routing explicit config updates locally without exposing values to
    the backend CLI.
    """
    if not text:
        return {}

    updates: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}

    for raw_line in lines:
        line = raw_line.lstrip("-* ").strip()
        match = _DIRECT_ENV_LINE.fullmatch(line)
        if not match:
            return {}

        key = match.group(1)
        value = match.group(2).strip()
        if key not in KNOWN_ENV_KEYS or not value:
            return {}

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            return {}

        if key == "AI_CLI_PATH":
            resolved = os.path.expanduser(value)
            if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
                return {}

        updates[key] = value

    return updates


def apply_env_updates(env_file: Path, updates: dict[str, str]) -> None:
    """Rewrite or append keys in ``env_file`` while preserving other lines."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_file.read_text().splitlines() if env_file.exists() else []
    written: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key, _old_value = line.split("=", 1)
        if key in updates:
            new_lines.append("%s=%s" % (key, updates[key]))
            written.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in written:
            new_lines.append("%s=%s" % (key, value))

    env_file.write_text("\n".join(new_lines).rstrip("\n") + "\n")
