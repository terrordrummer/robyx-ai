"""Robyx — Voice transcription via OpenAI Whisper (optional)."""

import logging
import os

import httpx

from config import OPENAI_API_KEY
from i18n import STRINGS

VOICE_TIMEOUT = int(os.environ.get("VOICE_TIMEOUT_SECONDS", "60"))

log = logging.getLogger("robyx.voice")


def is_available() -> bool:
    """Check if voice transcription is configured."""
    return bool(OPENAI_API_KEY)


async def transcribe_voice(file_path: str) -> tuple[str | None, str | None]:
    """Transcribe audio file via Whisper. Returns (text, error)."""
    if not OPENAI_API_KEY:
        return None, STRINGS["voice_no_key"]

    try:
        async with httpx.AsyncClient(timeout=VOICE_TIMEOUT) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": "Bearer %s" % OPENAI_API_KEY},
                    files={"file": ("voice.ogg", f, "audio/ogg")},
                    data={"model": "whisper-1"},
                )
            if response.status_code != 200:
                log.error("Whisper error: %s", response.text[:200])
                return None, STRINGS["voice_error"] % response.status_code
            return response.json().get("text", ""), None
    except (httpx.HTTPError, OSError, KeyError, ValueError) as e:
        log.error("Transcription error: %s", e)
        return None, STRINGS["voice_error"] % e
