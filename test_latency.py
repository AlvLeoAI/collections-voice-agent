from src.voice_handler import VoiceHandler
import time
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

def test_streaming():
    vh = VoiceHandler()
    text = "Hello! This is a test of the ultra low latency streaming system using MPV. If you can hear this instantly, it is working correctly."
    
    print(f"Start: {time.time()}")
    # Deberías escuchar audio casi inmediatamente después de este print
    vh.text_to_speech_stream(text)
    print(f"End: {time.time()}")

if __name__ == "__main__":
    test_streaming()