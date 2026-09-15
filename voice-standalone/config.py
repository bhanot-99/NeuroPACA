import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SAMPLE_RATE = 16000
STT_MODEL = "gemini-2.5-flash"
INTENT_MODEL = "gemini-2.5-flash"
