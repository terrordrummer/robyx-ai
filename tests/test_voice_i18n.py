"""Tests for voice transcription and i18n string modules."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from i18n import STRINGS
import voice
import config


# ═══════════════════════════════════════════════════════════════════════
# voice.is_available
# ═══════════════════════════════════════════════════════════════════════


class TestIsAvailable:
    def test_available_with_key(self):
        """is_available returns True when OPENAI_API_KEY is set."""
        assert voice.is_available() is True

    def test_unavailable_with_empty_key(self, monkeypatch):
        """is_available returns False when OPENAI_API_KEY is empty."""
        monkeypatch.setattr(config, "OPENAI_API_KEY", "")
        # voice reads from config at call time
        monkeypatch.setattr(voice, "OPENAI_API_KEY", "")
        assert voice.is_available() is False


# ═══════════════════════════════════════════════════════════════════════
# voice.transcribe_voice
# ═══════════════════════════════════════════════════════════════════════


class TestTranscribeVoice:
    @pytest.fixture
    def audio_file(self, tmp_path):
        """Create a fake audio file for testing."""
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"\x00\x01\x02\x03fake-ogg-data")
        return str(path)

    @pytest.fixture
    def mock_httpx_success(self):
        """Mock httpx client returning a successful transcription."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Hello world"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client, mock_response

    @pytest.mark.asyncio
    async def test_transcribe_success(self, audio_file, mock_httpx_success):
        """Successful transcription returns the text."""
        mock_client, _ = mock_httpx_success
        with patch("voice.httpx.AsyncClient", return_value=mock_client):
            text, error = await voice.transcribe_voice(audio_file)
        assert text == "Hello world"
        assert error is None

    @pytest.mark.asyncio
    async def test_transcribe_no_key(self, audio_file, monkeypatch):
        """Returns voice_no_key error when API key is missing."""
        monkeypatch.setattr(voice, "OPENAI_API_KEY", "")
        text, error = await voice.transcribe_voice(audio_file)
        assert text is None
        assert error == STRINGS["voice_no_key"]

    @pytest.mark.asyncio
    async def test_transcribe_api_error(self, audio_file):
        """Returns voice_error with status code on non-200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("voice.httpx.AsyncClient", return_value=mock_client):
            text, error = await voice.transcribe_voice(audio_file)
        assert text is None
        assert error == STRINGS["voice_error"] % 400

    @pytest.mark.asyncio
    async def test_transcribe_exception(self, audio_file):
        """Returns voice_error with exception message on httpx error."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("voice.httpx.AsyncClient", return_value=mock_client):
            text, error = await voice.transcribe_voice(audio_file)
        assert text is None
        assert error == STRINGS["voice_error"] % "connection refused"

    @pytest.mark.asyncio
    async def test_transcribe_empty_text(self, audio_file):
        """Returns empty string when API response has no text field."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("voice.httpx.AsyncClient", return_value=mock_client):
            text, error = await voice.transcribe_voice(audio_file)
        assert text == ""
        assert error is None

    @pytest.mark.asyncio
    async def test_transcribe_correct_api_params(self, audio_file, mock_httpx_success, monkeypatch):
        """Verify the correct API URL, headers, and upload params."""
        monkeypatch.setattr(voice, "OPENAI_API_KEY", "test-openai-key")
        mock_client, _ = mock_httpx_success

        with patch("voice.httpx.AsyncClient", return_value=mock_client):
            await voice.transcribe_voice(audio_file)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args

        # Check URL
        assert call_args[0][0] == "https://api.openai.com/v1/audio/transcriptions"

        # Check authorization header
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-openai-key"

        # Check model in data — no language param (auto-detect)
        data = call_args[1]["data"]
        assert data["model"] == "whisper-1"
        assert "language" not in data

        # Check file upload key
        files = call_args[1]["files"]
        assert "file" in files
        file_tuple = files["file"]
        assert file_tuple[0] == "voice.ogg"
        assert file_tuple[2] == "audio/ogg"


# ═══════════════════════════════════════════════════════════════════════
# i18n.STRINGS
# ═══════════════════════════════════════════════════════════════════════


EXPECTED_KEYS = [
    "unauthorized", "empty_message", "bot_alive",
    "no_agents", "agents_title", "agent_spawned", "agent_not_found",
    "agent_killed", "agent_kill_failed", "agent_reset",
    "focus_active", "focus_none", "focus_on", "focus_off", "focus_off_was",
    "workspaces_title", "no_workspaces", "workspace_created", "workspace_closed",
    "specialists_title", "no_specialists", "specialist_created",
    "ai_timeout", "ai_idle_timeout", "ai_empty", "ai_no_response", "ai_error",
    "rate_limited", "network_error", "permission_denied", "session_expired",
    "scheduler_dispatched", "scheduler_skipped", "scheduler_idle",
    "delegation_sent", "delegation_agent_missing", "delegation_result",
    "restart_pending",
    "voice_no_key", "voice_error", "voice_transcript",
    "help_text",
    "update_available", "update_available_breaking", "update_available_incompatible",
    "update_checking", "update_none", "update_applying", "update_migration",
    "update_migration_step", "update_success", "update_failed",
    "update_fetch_error", "update_no_pending", "update_auto_applying", "update_auto_failed",
    "time_now", "time_minutes", "time_hours", "time_days",
]


class TestI18nStrings:
    @pytest.mark.parametrize("key", EXPECTED_KEYS)
    def test_expected_key_present(self, key):
        """Every expected key must exist in STRINGS."""
        assert key in STRINGS, f"Missing key: {key}"

    def test_no_none_values(self):
        """No string value should be None."""
        for key, value in STRINGS.items():
            assert value is not None, f"STRINGS['{key}'] is None"

    def test_all_values_non_empty(self):
        """All string values must be non-empty."""
        for key, value in STRINGS.items():
            assert len(value) > 0, f"STRINGS['{key}'] is empty"

    def test_all_values_are_strings(self):
        """All values in STRINGS must be str type."""
        for key, value in STRINGS.items():
            assert isinstance(value, str), f"STRINGS['{key}'] is {type(value)}, expected str"

    def test_format_specifiers_valid(self):
        """Keys with %s or %d should have valid Python format specifiers."""
        import re
        format_pattern = re.compile(r"%[sd]")
        for key, value in STRINGS.items():
            matches = format_pattern.findall(value)
            # Just verify these are parseable — no stray lone %
            # (a lone % not followed by s/d/% would be an error)
            try:
                # Replace all valid format specs with dummy values to check syntax
                test_val = value
                test_val = test_val.replace("%s", "X").replace("%d", "0").replace("%%", "%")
                # If there's still a stray % followed by something unexpected, this is suspicious
                stray = re.findall(r"%[^sd%\s]", value)
                # Allow %s %d %% only
                assert not stray, (
                    f"STRINGS['{key}'] has invalid format specifier(s): {stray}"
                )
            except Exception as e:
                pytest.fail(f"STRINGS['{key}'] format check failed: {e}")

    def test_spot_check_unauthorized(self):
        assert STRINGS["unauthorized"] == "Unauthorized."

    def test_spot_check_voice_no_key(self):
        assert "OPENAI_API_KEY" in STRINGS["voice_no_key"]
        assert ".env" in STRINGS["voice_no_key"]

    def test_spot_check_voice_error_format(self):
        """voice_error should accept a single %s substitution."""
        result = STRINGS["voice_error"] % "test-error"
        assert "test-error" in result

    def test_spot_check_bot_alive_format(self):
        """bot_alive should accept %d and %s."""
        result = STRINGS["bot_alive"] % (3, " (focused)")
        assert "3" in result

    def test_spot_check_time_now(self):
        assert STRINGS["time_now"] == "now"

    def test_spot_check_help_text_contains_commands(self):
        """help_text should mention key bot commands."""
        text = STRINGS["help_text"]
        assert "/workspaces" in text
        assert "/status" in text
        assert "/focus" in text
        assert "/ping" in text
