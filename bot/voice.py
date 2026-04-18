"""Robyx — Voice transcription via OpenAI Whisper (optional)."""

import logging
import os

import httpx

from config import OPENAI_API_KEY
from i18n import STRINGS

VOICE_TIMEOUT = int(os.environ.get("VOICE_TIMEOUT_SECONDS", "60"))

log = logging.getLogger("robyx.voice")

# OpenAI Whisper hard-caps audio uploads at 25 MB per request. We enforce
# the same ceiling before hitting the network so an oversize file produces
# an immediate, actionable user-facing error instead of a slow 413 round
# trip. Mirrors the ingestion-side caps in Pass 2 P2-82 (Telegram),
# P2-11 (Discord), P2-72 (updater restore).
_MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024


def is_available() -> bool:
    """Check if voice transcription is configured."""
    return bool(OPENAI_API_KEY)


async def transcribe_voice(file_path: str) -> tuple[str | None, str | None]:
    """Transcribe audio file via Whisper. Returns (text, error)."""
    if not OPENAI_API_KEY:
        return None, STRINGS["voice_no_key"]

    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        log.error("Transcription error reading %s: %s", file_path, e)
        return None, STRINGS["voice_error"] % e
    if size > _MAX_TRANSCRIPTION_BYTES:
        log.warning(
            "Refusing to transcribe oversized voice file: %d bytes (cap %d)",
            size, _MAX_TRANSCRIPTION_BYTES,
        )
        return None, STRINGS["voice_too_large"] % (
            size // (1024 * 1024),
            _MAX_TRANSCRIPTION_BYTES // (1024 * 1024),
        )

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
