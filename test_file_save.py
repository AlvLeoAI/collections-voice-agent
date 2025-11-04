from src.voice_handler import VoiceHandler
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

def test_file_generation():
    vh = VoiceHandler()
    text = "This is a test recording to verify that the file saving logic is working correctly."
    
    # This will save a file in recordings/test_output.mp3
    output_path = "recordings/test_output.mp3"
    
    print(f"Generating audio file: {output_path}...")
    vh.text_to_speech(text, output_path=output_path)
    
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        print(f"Success! File saved and has size: {os.path.getsize(output_path)} bytes")
    else:
        print("Error: File was not saved or is empty.")

if __name__ == "__main__":
    test_file_generation()
