from google import genai
from google.genai import types

from config import GEMINI_API_KEY, STT_MODEL

_client = genai.Client(api_key=GEMINI_API_KEY)


def transcribe(wav_path: str) -> str:
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
    response = _client.models.generate_content(
        model=STT_MODEL,
        contents=[
            "Transcribe the spoken words in this audio exactly as said. "
            "Output only the transcription, nothing else.",
            audio_part,
        ],
    )
    return (response.text or "").strip()
