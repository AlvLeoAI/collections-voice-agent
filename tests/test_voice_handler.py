from __future__ import annotations

import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from src.voice_handler import (
    MAX_INLINE_AUDIO_BYTES,
    VoiceHandler,
    VoiceNotConfiguredError,
)


class _StubInteractions:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class _StubGenAIClient:
    def __init__(self, output_text: str) -> None:
        self.interactions = _StubInteractions(output_text)


class _StubTextToSpeech:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    def convert(self, **kwargs: Any) -> list[bytes]:
        self.calls.append(kwargs)
        return list(self.chunks)


class _StubElevenLabsClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self.text_to_speech = _StubTextToSpeech(chunks)


def test_zero_key_instantiation_disables_voice_safely() -> None:
    vh = VoiceHandler()
    assert vh.stt_enabled is False
    assert vh.tts_enabled is False
    assert vh.stt_model == "gemini-3.5-transcribe"


def test_unconfigured_stt_and_tts_raise_voice_not_configured_error() -> None:
    vh = VoiceHandler()
    with pytest.raises(VoiceNotConfiguredError):
        vh.transcribe_audio_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    with pytest.raises(VoiceNotConfiguredError):
        vh.text_to_speech_bytes("Hello there.")


def test_gemini_stt_uses_interactions_api_store_false_and_never_logs_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_transcript = "My ZIP code is 78701 and I can pay Friday."
    stub_client = _StubGenAIClient(output_text=f"  {secret_transcript}  ")
    vh = VoiceHandler(genai_client=stub_client)
    assert vh.stt_enabled is True

    raw_audio = b"fake-wav-payload-bytes"
    with caplog.at_level(logging.INFO, logger="src.voice_handler"):
        result = vh.transcribe_audio_bytes(raw_audio, mime_type="audio/wav", language_code="en-US")

    assert result == secret_transcript
    assert len(stub_client.interactions.calls) == 1
    call_kwargs = stub_client.interactions.calls[0]
    assert call_kwargs["model"] == "gemini-3.5-transcribe"
    assert call_kwargs["store"] is False
    assert call_kwargs["input"] == [
        {
            "type": "audio",
            "data": base64.b64encode(raw_audio).decode("utf-8"),
            "mime_type": "audio/wav",
        }
    ]
    assert call_kwargs["generation_config"] == {
        "transcription_config": {
            "language_codes": ["en-US"],
        }
    }

    log_text = caplog.text
    assert "STT completed" in log_text
    assert secret_transcript not in log_text
    assert "78701" not in log_text


def test_transcribe_audio_detects_mp3_mime_type(tmp_path: Path) -> None:
    audio_file = tmp_path / "sample.mp3"
    audio_file.write_bytes(b"ID3-fake-mp3-data")

    stub_client = _StubGenAIClient(output_text="Yes, speaking.")
    vh = VoiceHandler(genai_client=stub_client)
    result = vh.transcribe_audio(str(audio_file))

    assert result == "Yes, speaking."
    call_kwargs = stub_client.interactions.calls[0]
    assert call_kwargs["input"][0]["mime_type"] == "audio/mp3"


def test_transcribe_audio_bytes_validates_empty_and_size_limit() -> None:
    vh = VoiceHandler(genai_client=_StubGenAIClient(output_text="ok"))
    with pytest.raises(ValueError, match="empty"):
        vh.transcribe_audio_bytes(b"")
    with pytest.raises(ValueError, match="20 MB"):
        vh.transcribe_audio_bytes(b"x" * (MAX_INLINE_AUDIO_BYTES + 1))


def test_elevenlabs_tts_bytes_joins_chunks_and_never_logs_assistant_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_assistant_text = "Can you pay the $240.00 balance today?"
    stub_client = _StubElevenLabsClient(chunks=[b"chunk1-", b"chunk2"])
    vh = VoiceHandler(elevenlabs_client=stub_client)
    assert vh.tts_enabled is True

    with caplog.at_level(logging.INFO, logger="src.voice_handler"):
        audio = vh.text_to_speech_bytes(secret_assistant_text)

    assert audio == b"chunk1-chunk2"
    assert len(stub_client.text_to_speech.calls) == 1
    assert stub_client.text_to_speech.calls[0]["model_id"] == "eleven_turbo_v2_5"

    log_text = caplog.text
    assert "TTS generated" in log_text
    assert secret_assistant_text not in log_text
    assert "240.00" not in log_text
