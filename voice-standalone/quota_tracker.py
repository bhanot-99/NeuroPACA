"""
Tracks whether Gemini's free-tier daily quota is known to be exhausted,
so the cascade in llm_intent.py can skip straight to the local model for
the rest of the day instead of paying a network round-trip to fail again.

Deliberately does NOT count requests against a guessed daily number —
Google no longer publishes a flat RPD figure (checked directly against
ai.google.dev's rate-limits page, 2026-09-15: it now says limits are
account/tier-specific, visible only in the AI Studio dashboard, not a
static number worth hardcoding and having go stale). Instead this reacts
to the real signal: a 429 from the API. One JSON file, one field.

Persisted (not in-memory) so a daemon restart mid-day doesn't forget
today's exhaustion and waste another call finding out again.
"""

import json
import os
import time

_STATE_PATH = os.path.expanduser("~/.local/share/voice-standalone/gemini_quota.json")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def is_exhausted_today() -> bool:
    try:
        with open(_STATE_PATH, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return state.get("exhausted_on") == _today()


def mark_exhausted() -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, "w") as f:
        json.dump({"exhausted_on": _today()}, f)
