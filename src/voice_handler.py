from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_INLINE_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB

_SUFFIX_TO_MIME: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".m4a": "audio/m4a",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


class VoiceNotConfiguredError(RuntimeError):
    """Raised when an optional voice provider is invoked without credentials or SDK."""


class VoiceHandler:
    """Optional, lazy-loaded voice adapter (Gemini 3.5 Transcribe STT + ElevenLabs TTS)."""

    def __init__(
        self,
        gemini_api_key: str | None = None,
        elevenlabs_api_key: str | None = None,
        stt_model: str | None = None,
        voice_id: str | None = None,
        genai_client: Any = None,
        elevenlabs_client: Any = None,
    ) -> None:
        self._gemini_api_key = (
            gemini_api_key
            if gemini_api_key is not None
            else (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "")
        ).strip()
        self._elevenlabs_api_key = (
            elevenlabs_api_key
            if elevenlabs_api_key is not None
            else (os.getenv("ELEVENLABS_API_KEY") or "")
        ).strip()
        self.stt_model = (stt_model or os.getenv("GEMINI_STT_MODEL") or "gemini-3.5-transcribe").strip()
        self.voice_id = (voice_id or os.getenv("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM").strip()
        self._genai_client = genai_client
        self._elevenlabs_client = elevenlabs_client
        self.mpv_available = shutil.which("mpv") is not None

    @property
    def stt_enabled(self) -> bool:
        return self._genai_client is not None or bool(self._gemini_api_key)

    @property
    def tts_enabled(self) -> bool:
        return self._elevenlabs_client is not None or bool(self._elevenlabs_api_key)

    def _get_genai_client(self) -> Any:
        if self._genai_client is not None:
            return self._genai_client
        if not self._gemini_api_key:
            raise VoiceNotConfiguredError("Gemini STT is not configured. Set GEMINI_API_KEY.")
        try:
            from google import genai
        except ImportError as exc:
            raise VoiceNotConfiguredError(
                "google-genai is not installed. Run: uv sync --extra voice"
            ) from exc
        self._genai_client = genai.Client(api_key=self._gemini_api_key)
        return self._genai_client

    def _get_elevenlabs_client(self) -> Any:
        if self._elevenlabs_client is not None:
            return self._elevenlabs_client
        if not self._elevenlabs_api_key:
            raise VoiceNotConfiguredError(
                "ElevenLabs TTS is not configured. Set ELEVENLABS_API_KEY."
            )
        try:
            from elevenlabs import ElevenLabs
        except ImportError as exc:
            raise VoiceNotConfiguredError(
                "elevenlabs is not installed. Run: uv sync --extra voice"
            ) from exc
        self._elevenlabs_client = ElevenLabs(api_key=self._elevenlabs_api_key)
        return self._elevenlabs_client

    def transcribe_audio_bytes(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str = "audio/wav",
        language_code: str = "en-US",
    ) -> str:
        """Transcribe inline audio bytes via Gemini Interactions API with store=False."""
        if not audio_bytes:
            raise ValueError("Audio payload is empty.")
        if len(audio_bytes) > MAX_INLINE_AUDIO_BYTES:
            raise ValueError("Audio payload exceeds 20 MB inline limit.")

        client = self._get_genai_client()
        started = time.perf_counter()
        interaction = client.interactions.create(
            model=self.stt_model,
            input=[
                {
                    "type": "audio",
                    "data": base64.b64encode(audio_bytes).decode("utf-8"),
                    "mime_type": mime_type,
                }
            ],
            generation_config={
                "transcription_config": {
                    "language_codes": [language_code],
                }
            },
            store=False,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        transcript_text = (getattr(interaction, "output_text", None) or "").strip()
        word_count = len(transcript_text.split()) if transcript_text else 0
        logger.info(
            "STT completed provider=gemini model=%s mime_type=%s bytes=%d chars=%d words=%d latency_ms=%.1f",
            self.stt_model,
            mime_type,
            len(audio_bytes),
            len(transcript_text),
            word_count,
            latency_ms,
        )
        return transcript_text

    def transcribe_audio(self, audio_file_path: str, *, language_code: str = "en-US") -> str:
        """Read an audio file from disk and transcribe via Gemini STT."""
        path = Path(audio_file_path)
        mime_type = _SUFFIX_TO_MIME.get(path.suffix.lower(), "audio/wav")
        audio_bytes = path.read_bytes()
        return self.transcribe_audio_bytes(
            audio_bytes,
            mime_type=mime_type,
            language_code=language_code,
        )

    def _build_voice_settings(self) -> Any:
        try:
            from elevenlabs import VoiceSettings

            return VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True,
            )
        except ImportError:
            return {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            }

    def text_to_speech_bytes(self, text: str) -> bytes:
        """Synthesize speech audio bytes via ElevenLabs without persisting raw text."""
        if not text or not text.strip():
            raise ValueError("TTS text must not be empty.")

        client = self._get_elevenlabs_client()
        audio_stream = client.text_to_speech.convert(
            voice_id=self.voice_id,
            text=text,
            model_id="eleven_turbo_v2_5",
            voice_settings=self._build_voice_settings(),
        )
        if isinstance(audio_stream, (bytes, bytearray)):
            audio_bytes = bytes(audio_stream)
        else:
            audio_bytes = b"".join(chunk for chunk in audio_stream if chunk)

        logger.info(
            "TTS generated provider=elevenlabs chars=%d bytes=%d",
            len(text),
            len(audio_bytes),
        )
        return audio_bytes

    def text_to_speech(self, text: str, output_path: str | None = None) -> str:
        """Synthesize speech and write MP3 bytes to a deterministic local file path."""
        audio_bytes = self.text_to_speech_bytes(text)
        if output_path is None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            output_path = f"recordings/response_{digest}.mp3"

        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio_bytes)
        return output_path

    def text_to_speech_stream(self, text: str) -> None:
        """Optional local CLI playback via mpv; falls back to file-based TTS if mpv is absent."""
        if not self.mpv_available:
            self.text_to_speech(text)
            return

        audio_bytes = self.text_to_speech_bytes(text)
        mpv_command = ["mpv", "--no-cache", "--no-terminal", "--", "-"]
        process = subprocess.Popen(
            mpv_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if process.stdin is not None:
                process.stdin.write(audio_bytes)
                process.stdin.flush()
                process.stdin.close()
            process.wait()
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise
        finally:
            if process.poll() is None:
                process.wait(timeout=2)
                if process.poll() is None:
                    process.kill()

    def get_available_voices(self) -> list[tuple[str, str]]:
        """Get list of available ElevenLabs voices."""
        try:
            client = self._get_elevenlabs_client()
            voices = client.voices.get_all()
            return [(v.voice_id, v.name) for v in voices.voices]
        except Exception as exc:
            logger.error("Error fetching voices: %s", exc.__class__.__name__)
            return []
