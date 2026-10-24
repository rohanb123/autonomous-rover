"""Audio → text for voice missions.

Grok speech-to-text (xAI `POST /v1/stt`) when online, biased toward our rock vocabulary with
`keyterm`. Offline backup: faster-whisper on the GX10 (optional install, docs/05 U-15).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile

import httpx

from brain import settings

log = logging.getLogger(__name__)

XAI_STT_URL = "https://api.x.ai/v1/stt"
_EXT = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
}


class AsrUnavailable(RuntimeError):
    """Neither Grok nor the offline backup could transcribe: type the mission instead."""


def _grok(audio: bytes, mime: str) -> str:
    v = settings.load().voice
    key = settings.env("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    ext = _EXT.get(mime.split(";")[0].strip(), "webm")
    # Repeated form fields (keyterm) go as a list; httpx rejects a list of pairs with files.
    data = {"model": v.asr_model, "language": v.asr_language, "keyterm": list(v.asr_keyterms)}
    resp = httpx.post(
        XAI_STT_URL,
        headers={"Authorization": f"Bearer {key}"},
        data=data,
        files={"file": (f"mission.{ext}", audio, mime)},  # file must be the last part
        timeout=v.asr_timeout_s,
    )
    resp.raise_for_status()
    return resp.json()["text"].strip()


_whisper_model = None


def _whisper(audio: bytes) -> str:
    """ffmpeg → 16 kHz mono wav → faster-whisper. Loaded on first use only."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise AsrUnavailable("faster-whisper is not installed (offline voice backup)") from e
    v = settings.load().voice
    if _whisper_model is None:
        _whisper_model = WhisperModel(v.asr_whisper_size, device="auto", compute_type="int8")
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-ar",
                "16000",
                "-ac",
                "1",
                wav.name,
            ],
            input=audio,
            check=True,
        )
        segments, _ = _whisper_model.transcribe(wav.name, language=v.asr_language)
        return " ".join(s.text.strip() for s in segments).strip()


def transcribe_ex(audio: bytes, mime: str = "audio/webm") -> tuple[str, str]:
    """(text, source) where source is 'grok' or 'faster-whisper'."""
    try:
        return _grok(audio, mime), "grok"
    except Exception as e:
        log.warning("Grok STT failed (%s); trying faster-whisper", e)
    try:
        return _whisper(audio), "faster-whisper"
    except AsrUnavailable:
        raise
    except Exception as e:
        raise AsrUnavailable(f"offline transcription failed: {e}") from e


def transcribe(audio: bytes, mime: str = "audio/webm") -> str:
    """Audio bytes → text. Grok first, faster-whisper backup. Raises AsrUnavailable."""
    return transcribe_ex(audio, mime)[0]
