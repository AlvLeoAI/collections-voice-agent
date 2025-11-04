import os
import subprocess
import shutil
from openai import OpenAI
from elevenlabs import ElevenLabs, VoiceSettings
from pathlib import Path
from typing import Optional, List, Tuple
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class VoiceHandler:
    """Handles Speech-to-Text and Text-to-Speech operations with low-latency streaming"""
    
    def __init__(self):
        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
        self.voice_id = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
        
        # Dependency Check for mpv
        self.mpv_available = shutil.which('mpv') is not None
        if not self.mpv_available:
            logger.warning("mpv not found in system path. Real-time streaming will fall back to file-based TTS.")
        else:
            logger.info("mpv detected. Ultra-low latency streaming enabled.")
        
    def transcribe_audio(self, audio_file_path: str) -> str:
        """
        Transcribe audio file to text using OpenAI Whisper
        
        Args:
            audio_file_path: Path to audio file
            
        Returns:
            Transcribed text
        """
        try:
            with open(audio_file_path, "rb") as audio_file:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="en"
                )
            logger.info(f"Transcribed: {transcript.text}")
            return transcript.text
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise
    
    def text_to_speech(self, text: str, output_path: Optional[str] = None) -> str:
        """
        Convert text to speech using ElevenLabs (Standard file-based method)
        
        Args:
            text: Text to convert to speech
            output_path: Optional path to save audio file
            
        Returns:
            Path to generated audio file
        """
        try:
            logger.info(f"Generating speech for: {text[:50]}...")
            
            # Generate audio
            audio_generator = self.elevenlabs_client.text_to_speech.convert(
                voice_id=self.voice_id,
                text=text,
                model_id="eleven_turbo_v2_5",
                voice_settings=VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.75,
                    style=0.0,
                    use_speaker_boost=True
                )
            )
            
            # Save audio to file
            if output_path is None:
                # Use a deterministic name based on hash to avoid duplicates
                output_path = f"recordings/response_{abs(hash(text))}.mp3"
            
            # Ensure directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            
            # Write audio data
            with open(output_path, "wb") as f:
                for chunk in audio_generator:
                    f.write(chunk)
            
            logger.info(f"Audio saved to: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"TTS error: {e}")
            raise
    
    def text_to_speech_stream(self, text: str) -> None:
        """
        Stream text to speech directly to mpv for ultra-low latency playback.
        Falls back to file-based TTS if mpv is not available.
        
        Args:
            text: Text to convert to speech
        """
        if not self.mpv_available:
            logger.warning("Falling back to file-based playback as mpv is missing.")
            self.text_to_speech(text)
            return

        try:
            logger.info(f"Streaming audio directly to mpv for: {text[:50]}...")
            
            # Initialize ElevenLabs stream
            audio_generator = self.elevenlabs_client.text_to_speech.convert(
                voice_id=self.voice_id,
                text=text,
                model_id="eleven_turbo_v2_5",
                voice_settings=VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.75,
                )
            )

            # Direct streaming via MPV subprocess
            # --no-cache: minimize latency
            # --no-terminal: quiet output
            # --: stop option parsing
            # -: read from stdin
            mpv_command = ["mpv", "--no-cache", "--no-terminal", "--", "-"]
            process = subprocess.Popen(
                mpv_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            try:
                for chunk in audio_generator:
                    if chunk:
                        process.stdin.write(chunk)
                        process.stdin.flush()
                
                # Signal EOF to mpv
                if process.stdin:
                    process.stdin.close()
                
                # Wait for playback to finish
                process.wait()
                
            except Exception as e:
                logger.error(f"Error during audio piping: {e}")
                if process.poll() is None:
                    process.terminate()
                raise
            finally:
                # Ensure process is cleaned up
                if process.poll() is None:
                    process.wait(timeout=2)
                    if process.poll() is None:
                        process.kill()

        except Exception as e:
            logger.error(f"TTS streaming error: {e}")
            raise
    
    def get_available_voices(self) -> List[Tuple[str, str]]:
        """Get list of available ElevenLabs voices"""
        try:
            voices = self.elevenlabs_client.voices.get_all()
            return [(v.voice_id, v.name) for v in voices.voices]
        except Exception as e:
            logger.error(f"Error fetching voices: {e}")
            return []