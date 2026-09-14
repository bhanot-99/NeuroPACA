# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.2 · `VoiceCommand` and Tier-0 regex router (VISION_PHASES.md).

Deterministic, model-free pattern matching for common imperative commands
(e.g., "open Chrome", "close Firefox", "turn up the volume", "decrease the brightness").
Commands that do not match Tier 0 fall through to Tier 1 (grammar-constrained
span-pointer extraction via the interactive model).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VOLUME_UP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:turn\s+(?:the\s+)?volume\s+up|turn\s+up\s+(?:the\s+)?volume|volume\s+up|increase\s+(?:the\s+)?volume)\s*[.!?]?$",
    re.IGNORECASE,
)
_VOLUME_DOWN_RE = re.compile(
    r"^\s*(?:please\s+)?(?:turn\s+(?:the\s+)?volume\s+down|turn\s+down\s+(?:the\s+)?volume|volume\s+down|decrease\s+(?:the\s+)?volume)\s*[.!?]?$",
    re.IGNORECASE,
)
_BRIGHTNESS_UP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:turn\s+(?:the\s+)?brightness\s+up|turn\s+up\s+(?:the\s+)?brightness|brightness\s+up|increase\s+(?:the\s+)?brightness)\s*[.!?]?$",
    re.IGNORECASE,
)
_BRIGHTNESS_DOWN_RE = re.compile(
    r"^\s*(?:please\s+)?(?:turn\s+(?:the\s+)?brightness\s+down|turn\s+down\s+(?:the\s+)?brightness|brightness\s+down|decrease\s+(?:the\s+)?brightness)\s*[.!?]?$",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"^\s*(?:please\s+)?open\s+(?:(?:the|an?)\s+)?(?P<target>[^.!?]+?)\s*[.!?]?$",
    re.IGNORECASE,
)
_CLOSE_RE = re.compile(
    r"^\s*(?:please\s+)?close\s+(?:(?:the|an?)\s+)?(?P<target>[^.!?]+?)\s*[.!?]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    """One extracted voice command proposal."""

    action: str
    target: str | None
    source: str  # "pattern" | "model"


def try_pattern_match(text: str) -> VoiceCommand | None:
    """Tries deterministic regex patterns top-to-bottom.

    Returns the first matching `VoiceCommand` with source='pattern', or None
    if no pattern matches.
    """
    t = text.strip()
    if not t:
        return None

    if _VOLUME_UP_RE.match(t):
        return VoiceCommand(action="increase", target="volume", source="pattern")
    if _VOLUME_DOWN_RE.match(t):
        return VoiceCommand(action="decrease", target="volume", source="pattern")
    if _BRIGHTNESS_UP_RE.match(t):
        return VoiceCommand(action="increase", target="brightness", source="pattern")
    if _BRIGHTNESS_DOWN_RE.match(t):
        return VoiceCommand(action="decrease", target="brightness", source="pattern")

    m_open = _OPEN_RE.match(t)
    if m_open:
        target = m_open.group("target").strip()
        if target:
            return VoiceCommand(action="open", target=target, source="pattern")

    m_close = _CLOSE_RE.match(t)
    if m_close:
        target = m_close.group("target").strip()
        if target:
            return VoiceCommand(action="close", target=target, source="pattern")

    return None


# gen-ref: ff446a57
