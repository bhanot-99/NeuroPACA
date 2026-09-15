"""
Category J — Assistant meta/fallback (6 new skills; the other 2 catalog
items — "LLM fallback for unmatched requests" and "graceful I don't
understand" — already exist structurally as the pipeline's own fallback
chain and main.py's "No matching action" message, not separate skills).

  J04  sleep_stop_listening — "go to sleep", "stop listening"
  J05  wake_back_up         — "wake up"
  J06  report_version_status — "what version are you running", "are you working"
  J07  repeat_last_response  — "repeat that", "say that again"
  J08  cancel_action         — "cancel", "never mind"

sleep/wake and repeat need real session state (whether the assistant is
paused, and what it last said) — see skills/_session_state.py and main.py's
dispatch wrapper, which captures stdout so "repeat" doesn't require every
existing executor to be rewritten to return a string instead of printing.
"announce ready on startup" isn't a voice-triggered skill at all — it's
main.py's own startup print, enhanced there directly.
"""

import re
from collections.abc import Callable


def _match_sleep_stop_listening(text: str) -> dict | None:
    if re.search(r"\bgo\s+to\s+sleep\b|\bstop\s+listening\b|\bgo\s+to\s+sleep\s+mode\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_wake_back_up(text: str) -> dict | None:
    if re.search(r"\bwake\s+up\b|\bwake\s+back\s+up\b|\bstart\s+listening\s+again\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_report_version_status(text: str) -> dict | None:
    if re.search(
        r"\bwhat\s+version\s+are\s+you\s+running\b"
        r"|\bare\s+you\s+working\s+(?:properly|ok|okay)\b"
        r"|\bassistant\s+status\b"
        r"|\bhow\s+are\s+you\s+doing\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_repeat_last_response(text: str) -> dict | None:
    if re.search(r"\brepeat\s+that\b|\bsay\s+that\s+again\b|\bwhat\s+did\s+you\s+say\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_cancel_action(text: str) -> dict | None:
    if re.search(r"^\s*(?:cancel|never\s*mind|nevermind|stop)\s*[.!?]?\s*$",
                 text, re.IGNORECASE):
        return {}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("wake_back_up", _match_wake_back_up),
    ("sleep_stop_listening", _match_sleep_stop_listening),
    ("report_version_status", _match_report_version_status),
    ("repeat_last_response", _match_repeat_last_response),
    ("cancel_action", _match_cancel_action),
]
