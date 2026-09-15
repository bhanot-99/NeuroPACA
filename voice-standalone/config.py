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
# set_brightness. INTENT_MODEL is used for the LLM fallback in llm_intent.py.
# gemini-flash-lite-latest, not gemini-3.6-flash: checked the live model
# list directly (2026-09-16) — flash-lite is the smallest text-generation
# tier this API key has (Nano Banana is image generation despite the name;
# the Gemma models on this key are 26B-31B, larger, not smaller), and it
# benchmarked as accurate as full flash on real intent-resolution cases
# while giving noticeably more natural/conversational answer phrasing.
INTENT_MODEL = "gemini-flash-lite-latest"

# Layer-2 LLM intent fallback (llm_intent.py) — off by default. As of
# 2026-09-16 llm_intent.py is a real cascade, not just a single Gemini call:
# Gemini (flash-lite) first, and on ANY failure — quota 429, a deprecated
# model, network — it falls through to a fully local Qwen2.5-1.5B model via
# Ollama instead of giving up (benchmarked directly: 1.5B beat 3B on both
# speed and accuracy, so that's what's used, not the larger model). A 429
# specifically also gets remembered for the rest of the day (quota_tracker.py)
# so later utterances skip straight to local instead of re-paying a network
# round-trip to fail again. This removes the original reason this flag was
# off (quota exhaustion used to be a dead end) — still defaulting to off
# because flipping it is a real behavior change (unmatched utterances now
# always try an LLM, cloud or local, instead of silently doing nothing) and
# that's your call, same as SAFETY_TIERS_ENABLED below. When off, an
# utterance that layer0 (regex) and layer1 (local semantic matcher) can't
# resolve is just reported as "no matching action."
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

