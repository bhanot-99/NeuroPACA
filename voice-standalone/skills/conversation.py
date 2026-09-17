"""
Category K — Small-Talk & Conversational Fast-Path.
Exposes:
  - small_talk / conversation:
      * Matches common greetings and casual conversational phrases:
        - "how are you", "how are you doing", "hello how are you"
        - "hello", "hi", "hey", "greetings"
        - "good morning", "good afternoon", "good evening"
        - "what's up", "sup"
        - "who are you", "what is your name"
        - "thank you", "thanks"
      * Returns friendly conversational replies, preventing unnecessary web searches or model calls.
"""

import re
from collections.abc import Callable

_CONVERSATION_RESPONSES: list[tuple[re.Pattern, str]] = [
    # How are you (with optional preceding greeting like "hello how are you")
    (
        re.compile(
            r"^\s*(?:(?:hey|hi|hello)\s+)?how\s+(?:are\s+you(?:\s+doing)?|is\s+it\s+going|do\s+you\s+do|are\s+things)(?:\s+today)?\s*[?!.]*\s*$",
            re.IGNORECASE,
        ),
        "I'm doing well, thank you! How can I help you?",
    ),
    (
        re.compile(
            r"^\s*(?:(?:hey|hi|hello)\s+)?(?:how's\s+it\s+going|how're\s+you)(?:\s+today)?\s*[?!.]*\s*$",
            re.IGNORECASE,
        ),
        "I'm doing well, thank you! How can I help you?",
    ),
    # Who are you / what is your name
    (
        re.compile(
            r"^\s*(?:who|what)\s+are\s+you\s*[?!.]*\s*$"
            r"|^\s*what(?:'s|\s+is)\s+your\s+name\s*[?!.]*\s*$"
            r"|^\s*who\s+made\s+you\s*[?!.]*\s*$",
            re.IGNORECASE,
        ),
        "I'm NeuroPaca, your local AI voice assistant. How can I help you?",
    ),
    # What's up / sup
    (
        re.compile(
            r"^\s*(?:what(?:'s|\s+is)\s+up|sup)(?:\s+there)?\s*[?!.]*\s*$",
            re.IGNORECASE,
        ),
        "Not much, just ready to help! What's on your mind?",
    ),
    # Good morning / afternoon / evening
    (
        re.compile(
            r"^\s*good\s+morning(?:\s+there)?\s*[!.]*\s*$",
            re.IGNORECASE,
        ),
        "Good morning! How can I help you today?",
    ),
    (
        re.compile(
            r"^\s*good\s+afternoon(?:\s+there)?\s*[!.]*\s*$",
            re.IGNORECASE,
        ),
        "Good afternoon! How can I assist you?",
    ),
    (
        re.compile(
            r"^\s*good\s+evening(?:\s+there)?\s*[!.]*\s*$",
            re.IGNORECASE,
        ),
        "Good evening! How can I help you?",
    ),
    # Greetings: hello, hi, hey
    (
        re.compile(
            r"^\s*(?:hello|hi|hey|greetings)(?:\s+(?:there|assistant|neuropaca))?\s*[!.]*\s*$",
            re.IGNORECASE,
        ),
        "Hello! How can I help you today?",
    ),
    # Thanks / thank you
    (
        re.compile(
            r"^\s*(?:thank\s+you(?:\s+very\s+much)?|thanks(?:\s+a\s+lot)?)\s*[!.]*\s*$",
            re.IGNORECASE,
        ),
        "You're very welcome!",
    ),
]


def _match_small_talk(text: str) -> dict | None:
    """Matches common greetings and small-talk utterances, returning a friendly reply."""
    clean = text.strip()
    for pattern, reply in _CONVERSATION_RESPONSES:
        if pattern.search(clean):
            return {"reply": reply}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("small_talk", _match_small_talk),
]
