import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SAMPLE_RATE = 16000
# gemini-2.5-flash was deprecated for this API key ("no longer available to
# new users") — found via a real 404 during live testing, 2026-09-15. The
# API's own error response explicitly recommended gemini-3.6-flash, and it's
# confirmed present in this key's live model list with full generateContent
# support — used directly rather than guessing among the many other
# available flash variants.
STT_MODEL = "gemini-3.6-flash"
INTENT_MODEL = "gemini-3.6-flash"
