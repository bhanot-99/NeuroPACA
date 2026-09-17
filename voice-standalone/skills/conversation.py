"""
Category K — Small-Talk & Conversational Fast-Path.
Exposes:
  - small_talk / conversation:
      * Matches common greetings, introductions, and casual conversational phrases:
        - "how are you", "how are you doing", "hello how are you"
        - "hello", "hi", "hey", "greetings"
        - "my name is <name>", "I am <name>", "call me <name>"
        - "good morning", "good afternoon", "good evening"
        - "what's up", "sup"
        - "who are you", "what is your name"
        - "thank you", "thanks"
      * Extracts and remembers the user's name across the active session.
      * Returns personalized, friendly conversational replies, preventing unnecessary web searches or model calls.
"""

import re
from collections.abc import Callable

from skills import _session_state

# Name extraction: extracts name from introductions like "my name is Jatin", "call me Jatin", "I am Jatin"
_NAME_PATTERN = re.compile(
    r"\b(?:my\s+name\s+is|call\s+me|name(?:\x27|\s+)s|(?:i\s+am|i\x27m)\s+(?!fine\b|good\b|well\b|doing\b|okay\b|ok\b|great\b|here\b|back\b|tired\b|happy\b|sad\b|ready\b|listening\b))\s*([A-Za-z]+)\b",
    re.IGNORECASE,
)

# Inquiries about assistant state / how are you
_HOW_ARE_YOU_RE = re.compile(
    r"\bhow\s+(?:are\s+you(?:\s+doing)?|is\s+it\s+going|do\s+you\s+do|are\s+things)(?:\s+today)?\b|\bhow(?:\x27|\s+)s\s+it\s+going\b|\bhow(?:\x27|\s+)re\s+you\b",
    re.IGNORECASE,
)

# Time-based greetings
_TIME_GREETING_RE = re.compile(
    r"\bgood\s+(morning|afternoon|evening)\b",
    re.IGNORECASE,
)

# Pure greetings
_GREETING_TOKEN_RE = re.compile(
    r"\b(?:hello|hi|hey|greetings)\b",
    re.IGNORECASE,
)

# Whole-utterance validator for composite greeting/introduction/how-are-you utterances
_CONVERSATION_GRAMMAR_RE = re.compile(
    r"(?:"
    r"\b(?:hello|hi|hey|greetings|good\s+(?:morning|afternoon|evening))\b|"
    r"\b(?:my\s+name\s+is|call\s+me|name(?:\x27|\s+)s|(?:i\s+am|i\x27m)\s+(?!fine\b|good\b|well\b|doing\b|okay\b|ok\b|great\b|here\b|back\b|tired\b|happy\b|sad\b|ready\b|listening\b))\s*[A-Za-z]+\b|"
    r"\bhow\s+(?:are\s+you(?:\s+doing)?|is\s+it\s+going|do\s+you\s+do|are\s+things)(?:\s+today)?\b|\bhow(?:\x27|\s+)s\s+it\s+going\b|\bhow(?:\x27|\s+)re\s+you\b|"
    r"\b(?:there|assistant|neuropaca|today|please|thanks|thank\s+you|nice\s+to\s+meet\s+you)\b|"
    r"[,\.\?!;:\s]+"
    r")+$",
    re.IGNORECASE,
)

# Specialized patterns (identity, what's up, thanks)
_SPECIAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Who are you / what is your name
    (
        re.compile(
            r"^\s*(?:who|what)\s+are\s+you\s*[?!.]*$"
            r"|^\s*what(?:'s|\s+is)\s+your\s+name\s*[?!.]*$"
            r"|^\s*who\s+made\s+you\s*[?!.]*$",
            re.IGNORECASE,
        ),
        "I'm NeuroPaca, your local AI voice assistant. How can I help you?",
    ),
    # What's up / sup
    (
        re.compile(
            r"^\s*(?:(?:hey|hi|hello)\s+)?(?:what(?:'s|\s+is)\s+up|sup)(?:\s+there)?\s*[?!.]*$",
            re.IGNORECASE,
        ),
        "Not much, just ready to help! What's on your mind?",
    ),
    # Thanks / thank you
    (
        re.compile(
            r"^\s*(?:thank\s+you(?:\s+very\s+much)?|thanks(?:\s+a\s+lot)?)\s*[!.]*$",
            re.IGNORECASE,
        ),
        "You're very welcome!",
    ),
]


def _match_small_talk(text: str) -> dict | None:
    """Matches common greetings, introductions, and small-talk utterances, returning a friendly reply."""
    clean = text.strip()
    if not clean:
        return None

    # Check specialized standalone patterns first (who are you, what's up, thanks)
    for pattern, default_reply in _SPECIAL_PATTERNS:
        if pattern.search(clean):
            active_name = _session_state.get_user_name()
            if active_name and "NeuroPaca" in default_reply:
                reply = f"I'm NeuroPaca, your local AI voice assistant. How can I help you, {active_name}?"
            elif active_name and "Not much" in default_reply:
                reply = f"Not much, {active_name}, just ready to help! What's on your mind?"
            elif active_name and "welcome" in default_reply:
                reply = f"You're very welcome, {active_name}!"
            else:
                reply = default_reply
            return {"reply": reply}

    # Extract user name if provided (e.g. "my name is Jatin", "I am Jatin")
    name_match = _NAME_PATTERN.search(clean)
    extracted_name = None
    if name_match:
        extracted_name = name_match.group(1).strip().capitalize()
        _session_state.set_user_name(extracted_name)

    active_name = extracted_name or _session_state.get_user_name()

    # Validate against conversation grammar (entire utterance must consist of greeting/intro/inquiry elements)
    if not _CONVERSATION_GRAMMAR_RE.match(clean):
        return None

    # Must contain at least a greeting token, intro/name, or inquiry
    has_how_are_you = bool(_HOW_ARE_YOU_RE.search(clean))
    has_greeting = bool(_GREETING_TOKEN_RE.search(clean))
    has_time_greeting = bool(_TIME_GREETING_RE.search(clean))
    has_intro = bool(name_match)

    if not (has_how_are_you or has_greeting or has_time_greeting or has_intro):
        return None

    # Synthesize friendly, personalized response
    if has_how_are_you:
        if active_name:
            reply = f"I'm doing well, {active_name}! How can I help you today?"
        else:
            reply = "I'm doing well, thank you! How can I help you?"
    elif has_intro:
        reply = f"Hello, {active_name}! Nice to meet you. How can I help you today?"
    elif has_time_greeting:
        time_match = _TIME_GREETING_RE.search(clean)
        time_str = time_match.group(1).lower() if time_match else "day"
        if active_name:
            reply = f"Good {time_str}, {active_name}! How can I help you today?"
        else:
            reply = f"Good {time_str}! How can I help you today?"
    elif has_greeting:
        if active_name:
            reply = f"Hello, {active_name}! How can I help you today?"
        else:
            reply = "Hello! How can I help you today?"
    else:
        reply = "I'm doing well, thank you! How can I help you?"

    res = {"reply": reply}
    if active_name:
        res["user_name"] = active_name
    return res


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("small_talk", _match_small_talk),
]
