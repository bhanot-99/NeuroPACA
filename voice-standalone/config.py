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

# Step 6 safety tiers — off by default. ARCHITECTURE.md: "Activation — off
# by default. Turned on tier-by-tier once the pipeline has run reliably for
# real, day-to-day use. Your call, exactly as already agreed." This flag is
# that switch — flipping it to True is the only thing that changes DANGEROUS
# skills from executing immediately to routing through the confirm-loop.
# The audit log stays on regardless — it's observability, not a gate.
SAFETY_TIERS_ENABLED = os.environ.get("SAFETY_TIERS_ENABLED", "false").lower() == "true"
