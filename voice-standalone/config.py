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
# STT now runs locally via faster-whisper (stt.py) — every command was
# burning a Gemini call just to transcribe audio, which exhausted the
# free-tier daily quota (20 requests/day) even for fully local skills like
# set_brightness. INTENT_MODEL is used for the LLM fallback in llm_intent.py,
# the last remaining Gemini path — gated by LLM_FALLBACK_ENABLED below.
INTENT_MODEL = "gemini-3.6-flash"

# Layer-2 LLM intent fallback (llm_intent.py) — off by default. Still hitting
# the free-tier daily quota (20 requests/day) even after STT moved local, so
# this is disabled until either the quota situation changes or fallback
# moves to a local model too. When off, an utterance that layer0 (regex) and
# layer1 (local semantic matcher) can't resolve is just reported as "no
# matching action" instead of trying Gemini.
LLM_FALLBACK_ENABLED = os.environ.get("LLM_FALLBACK_ENABLED", "false").lower() == "true"

# Step 6 safety tiers — off by default. ARCHITECTURE.md: "Activation — off
# by default. Turned on tier-by-tier once the pipeline has run reliably for
# real, day-to-day use. Your call, exactly as already agreed." This flag is
# that switch — flipping it to True is the only thing that changes DANGEROUS
# skills from executing immediately to routing through the confirm-loop.
# The audit log stays on regardless — it's observability, not a gate.
SAFETY_TIERS_ENABLED = os.environ.get("SAFETY_TIERS_ENABLED", "false").lower() == "true"

# Step 7 TTS voices: "melo" (Indian-accented English via MeloTTS, ~0.15-0.20s per sentence)
# or "kokoro" (American English via RealtimeTTS Kokoro, streaming).
TTS_ENGLISH_VOICE = os.environ.get("TTS_ENGLISH_VOICE", "melo").lower()

# Punjabi output via IndicF5 (diffusion voice cloning model, ~14.3s per phrase on CPU).
# Enabled by default; lazy-loaded only when Punjabi text is detected.
TTS_ENABLE_PUNJABI = os.environ.get("TTS_ENABLE_PUNJABI", "true").lower() == "true"

