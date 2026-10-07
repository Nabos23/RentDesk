"""
Voice note transcription using OpenAI Whisper.

WhatsApp sends voice notes as .ogg (Opus codec).
We download → convert to .wav (pydub) → send to Whisper API.
"""
from __future__ import annotations

import io
import os
import tempfile

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    Transcribe raw audio bytes using OpenAI Whisper.

    WhatsApp sends voice as audio/ogg;codecs=opus.
    Whisper accepts: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg.

    Args:
        audio_bytes: Raw audio data downloaded from WhatsApp.
        mime_type: MIME type from WhatsApp (e.g. "audio/ogg; codecs=opus").

    Returns:
        Transcribed text string, or empty string on failure.
    """
    if not audio_bytes:
        logger.warning("transcribe_audio_empty_bytes")
        return ""

    # Determine file extension for Whisper
    ext = _mime_to_ext(mime_type)

    try:
        client = _get_client()

        # Wrap bytes in a file-like object with a .name attribute
        # (OpenAI SDK needs a filename to infer format)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"voice_note.{ext}"

        response = await client.audio.transcriptions.create(
            model=settings.WHISPER_MODEL,
            file=audio_file,
            # Hint the language to improve accuracy for Hindi / Roman Hindi
            # "hi" covers Devanagari Hindi; Roman Hindi is often transcribed
            # as English mixing — Whisper handles this well without a forced lang
            language=None,  # auto-detect; handles English + Hindi well
            response_format="text",
        )

        text = response.strip() if isinstance(response, str) else (response.text or "").strip()
        logger.info("transcription_success", length=len(text), ext=ext)
        return text

    except Exception as e:
        logger.error("transcription_error", error=str(e), mime_type=mime_type)
        return ""


def _mime_to_ext(mime_type: str) -> str:
    """Map MIME type to a file extension Whisper understands."""
    mime_lower = mime_type.lower()
    mapping = {
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "mp4",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/x-m4a": "m4a",
        "audio/aac": "m4a",
        "audio/amr": "mp3",    # Whisper doesn't support AMR → treat as MP3 (may fail)
    }
    for key, ext in mapping.items():
        if key in mime_lower:
            return ext
    return "ogg"  # Default for WhatsApp voice notes
