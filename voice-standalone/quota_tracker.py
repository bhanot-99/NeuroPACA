"""
quota_tracker.py — Health & Quota Tracker for Multi-Provider API Cascade.

Tracks whether cloud API providers (Gemini, Groq, NVIDIA) are exhausted
due to 429 Rate Limits, ResourceExhausted errors, or network outages, so the
cascade in llm_intent.py can bypass exhausted providers instantly for the rest
of the calendar day without paying network round-trip timeouts or penalties.

Persisted at: ~/.local/share/voice-standalone/quota.json
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

_STATE_PATH = os.path.expanduser("~/.local/share/voice-standalone/quota.json")
_LEGACY_GEMINI_PATH = os.path.expanduser("~/.local/share/voice-standalone/gemini_quota.json")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state() -> Dict[str, Any]:
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Legacy migration fallback if quota.json does not exist yet
    if os.path.exists(_LEGACY_GEMINI_PATH):
        try:
            with open(_LEGACY_GEMINI_PATH, "r") as f:
                legacy = json.load(f)
                if legacy.get("exhausted_on"):
                    return {"gemini": {"exhausted_on": legacy["exhausted_on"], "timestamp": time.time()}}
        except Exception:
            pass

    return {}


def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    temp_path = f"{_STATE_PATH}.tmp"
    with open(temp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_path, _STATE_PATH)


def is_exhausted(provider: str = "gemini") -> bool:
    """Returns True if the specified provider was marked exhausted today."""
    state = _load_state()
    prov_data = state.get(provider.lower())
    if isinstance(prov_data, dict):
        return prov_data.get("exhausted_on") == _today()
    # Backward compatibility with single-dict legacy shape: {"exhausted_on": "..."}
    if provider.lower() == "gemini" and state.get("exhausted_on") == _today():
        return True
    return False


def is_exhausted_today(provider: str = "gemini") -> bool:
    """Alias for is_exhausted to maintain backward compatibility."""
    return is_exhausted(provider)


def mark_exhausted(provider: str = "gemini", reason: str = "") -> None:
    """Marks a provider as exhausted for the current calendar day with timestamp and reason."""
    state = _load_state()
    prov_key = provider.lower()
    state[prov_key] = {
        "exhausted_on": _today(),
        "timestamp": time.time(),
        "reason": str(reason),
    }
    _save_state(state)


def reset_quota(provider: Optional[str] = None) -> None:
    """Resets quota status for a specific provider, or all providers if None."""
    if provider is None:
        if os.path.exists(_STATE_PATH):
            try:
                os.remove(_STATE_PATH)
            except OSError:
                pass
        return

    state = _load_state()
    prov_key = provider.lower()
    if prov_key in state:
        del state[prov_key]
        _save_state(state)


def get_exhausted_providers() -> List[str]:
    """Returns a list of all providers currently marked exhausted today."""
    state = _load_state()
    today_str = _today()
    exhausted = []
    for prov, data in state.items():
        if isinstance(data, dict) and data.get("exhausted_on") == today_str:
            exhausted.append(prov)
    return exhausted
