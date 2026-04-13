"""Shared fixtures for Robyx test suite."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add bot/ to sys.path so we can import modules directly
BOT_DIR = Path(__file__).parent.parent / "bot"
sys.path.insert(0, str(BOT_DIR))

# ── Patch config before any bot module is imported ──
# We must patch environment BEFORE importing config.py, because it reads
# env vars at module level.


@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    """Provide a clean temporary environment for every test."""
    monkeypatch.delenv("CLAUDE_PERMISSION_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ROBYX_BOT_TOKEN=test-token\n"
        "ROBYX_CHAT_ID=-100999\n"
        "ROBYX_OWNER_ID=12345\n"
        "AI_BACKEND=claude\n"
        "AI_CLI_PATH=/usr/bin/claude\n"
        "ROBYX_WORKSPACE=%s\n"
        "OPENAI_API_KEY=test-openai-key\n"
        "SCHEDULER_INTERVAL=600\n" % (tmp_path / "workspace")
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)

    # Patch config module attributes so all imports see tmp paths
    import config as cfg

    data_dir = tmp_path / "data"
    monkeypatch.setattr(cfg, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(cfg, "CHAT_ID", -100999)
    monkeypatch.setattr(cfg, "OWNER_ID", 12345)
    monkeypatch.setattr(cfg, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(cfg, "STATE_FILE", data_dir / "state.json")
    monkeypatch.setattr(cfg, "TASKS_FILE", data_dir / "tasks.md")
    monkeypatch.setattr(cfg, "SPECIALISTS_FILE", data_dir / "specialists.md")
    monkeypatch.setattr(cfg, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfg, "AGENTS_DIR", data_dir / "agents")
    monkeypatch.setattr(cfg, "SPECIALISTS_DIR", data_dir / "specialists")
    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "VERSION_FILE", tmp_path / "VERSION")
    monkeypatch.setattr(cfg, "RELEASES_DIR", tmp_path / "releases")
    monkeypatch.setattr(cfg, "UPDATES_STATE_FILE", data_dir / "updates.json")
    monkeypatch.setattr(cfg, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(cfg, "CLAUDE_PERMISSION_MODE", "")
    monkeypatch.setattr(cfg, "TIMED_QUEUE_FILE", data_dir / "timed_queue.json")
    monkeypatch.setattr(cfg, "QUEUE_FILE", data_dir / "queue.json")
    monkeypatch.setattr(cfg, "CONTINUOUS_DIR", data_dir / "continuous")
    monkeypatch.setattr(cfg, "DISCORD_BOT_TOKEN", "test-discord-token")
    monkeypatch.setattr(cfg, "DISCORD_GUILD_ID", None)
    monkeypatch.setattr(cfg, "DISCORD_OWNER_ID", None)
    monkeypatch.setattr(cfg, "DISCORD_CONTROL_CHANNEL_ID", None)

    # Also patch module-level copies in modules that do "from config import X"
    import agents as agents_mod
    import continuous as continuous_mod
    import scheduler as scheduler_mod
    import topics as topics_mod
    import voice as voice_mod

    monkeypatch.setattr(voice_mod, "OPENAI_API_KEY", "test-openai-key")

    monkeypatch.setattr(agents_mod, "STATE_FILE", data_dir / "state.json")
    monkeypatch.setattr(agents_mod, "WORKSPACE", tmp_path / "workspace")

    monkeypatch.setattr(continuous_mod, "CONTINUOUS_DIR", data_dir / "continuous")

    monkeypatch.setattr(scheduler_mod, "CLAIM_TIMEOUT_SECONDS", 300)
    monkeypatch.setattr(scheduler_mod, "MAX_REMINDER_ATTEMPTS", 10)
    monkeypatch.setattr(scheduler_mod, "TASKS_FILE", data_dir / "tasks.md")
    monkeypatch.setattr(scheduler_mod, "TIMED_QUEUE_FILE", data_dir / "timed_queue.json")
    monkeypatch.setattr(scheduler_mod, "QUEUE_FILE", data_dir / "queue.json")
    monkeypatch.setattr(scheduler_mod, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(scheduler_mod, "DATA_DIR", data_dir)

    monkeypatch.setattr(topics_mod, "SPECIALISTS_FILE", data_dir / "specialists.md")
    monkeypatch.setattr(topics_mod, "AGENTS_DIR", data_dir / "agents")
    monkeypatch.setattr(topics_mod, "SPECIALISTS_DIR", data_dir / "specialists")
    monkeypatch.setattr(topics_mod, "DATA_DIR", data_dir)

    data_dir.mkdir(exist_ok=True)
    (data_dir / "agents").mkdir(exist_ok=True)
    (data_dir / "specialists").mkdir(exist_ok=True)
    (tmp_path / "releases").mkdir(exist_ok=True)
    (tmp_path / "VERSION").write_text("0.1.0\n")


@pytest.fixture
def mock_platform():
    """A mock Platform with async methods."""
    p = AsyncMock()
    p.send_message = AsyncMock()
    p.reply = AsyncMock()
    p.edit_message = AsyncMock()
    p.send_typing = AsyncMock()
    p.download_voice = AsyncMock(return_value="/tmp/test.ogg")
    p.is_owner = MagicMock(return_value=True)
    # Default: thread_id None → main (matches Telegram semantics).
    p.is_main_thread = MagicMock(side_effect=lambda chat_id, thread_id: thread_id is None)
    # send_photo returns a MagicMock "sent message" by default. Tests that
    # need to simulate failure override with side_effect / return_value=None.
    p.send_photo = AsyncMock(return_value=MagicMock())
    p.max_photo_bytes = 10 * 1024 * 1024
    p.rename_main_channel = AsyncMock(return_value=True)
    p.create_channel = AsyncMock(return_value=999)
    p.close_channel = AsyncMock(return_value=True)
    p.send_to_channel = AsyncMock(return_value=True)
    p.max_message_length = 4000
    p.control_room_id = 1
    return p


@pytest.fixture
def mock_bot(mock_platform):
    """Alias for mock_platform (backward compat)."""
    return mock_platform


@pytest.fixture
def agent_manager(tmp_path, _patch_env):
    """A fresh AgentManager with patched paths."""
    from agents import AgentManager
    return AgentManager()


@pytest.fixture
def claude_backend():
    """A ClaudeBackend instance."""
    from ai_backend import ClaudeBackend
    return ClaudeBackend("/usr/bin/claude")


@pytest.fixture
def codex_backend():
    from ai_backend import CodexBackend
    return CodexBackend("/usr/bin/codex")


@pytest.fixture
def opencode_backend():
    from ai_backend import OpenCodeBackend
    return OpenCodeBackend("/usr/bin/opencode")
