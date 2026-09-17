"""
_compound_splitter.py — Deterministic Compound Command Pre-Processor.

Splits multi-action user utterances on conjunctions (" and ", ", and ", " then ", "; ")
into sequential atomic sub-commands, while preserving conjunctions that are internal
to single commands (such as search queries, email bodies, to-do items, or quoted strings).
"""

import re
from typing import List

# Conjunction split regex: splits on semicolons, ", and ", " and ", ", then ", " then "
_CONJUNCTION_PATTERN = re.compile(
    r"(?:;\s*|,\s*and\s+|\s+and\s+|,\s*then\s+|\s+then\s+)",
    re.IGNORECASE,
)

# Atomic command prefixes where "and"/"then" is treated as part of the query/payload, not a command delimiter
_ATOMIC_PREFIX_REGEX = re.compile(
    r"^\s*(?:"
    r"search\s+for\s+|"
    r"search\s+the\s+web\s+for\s+|"
    r"google\s+|"
    r"look\s+up\s+|"
    r"youtube\s+search\s+|"
    r"wikipedia\s+|"
    r"wiki\s+|"
    r"duckduckgo\s+|"
    r"send\s+(?:an\s+)?email\s+|"
    r"add\s+a?\s*to-?do|"
    r"remind\s+me\s+to\s+|"
    r"calculate\s+|"
    r"compute\s+|"
    r"define\s+|"
    r"spell\s+"
    r")",
    re.IGNORECASE,
)


def _is_atomic_command(text: str) -> bool:
    """Returns True if the utterance is an atomic skill that should not be split."""
    trimmed = text.strip()
    if _ATOMIC_PREFIX_REGEX.match(trimmed):
        return True
    # Email syntax check: "send ... to ... with subject ... and message/body ..."
    if "send" in trimmed.lower() and "email" in trimmed.lower() and "@" in trimmed:
        return True
    return False


def split_compound_utterance(text: str) -> List[str]:
    """Splits a compound command into atomic sub-commands.
    
    Examples:
        'set volume to 90% and brightness to 90%' -> ['set volume to 90%', 'brightness to 90%']
        'turn off wifi then lock screen' -> ['turn off wifi', 'lock screen']
        'battery status; current time' -> ['battery status', 'current time']
        'search for tom and jerry' -> ['search for tom and jerry']
    """
    if not text or not text.strip():
        return []

    clean_text = text.strip()

    # Do not split if the command is inherently an atomic query (search, email, todo, etc.)
    if _is_atomic_command(clean_text):
        return [clean_text]

    # Mask quoted strings to prevent splitting inside quotes
    quotes: List[str] = []
    def _mask_quote(m):
        quotes.append(m.group(0))
        return f"__QUOTE_{len(quotes)-1}__"

    masked = re.sub(r'("[^"]*"|\'[^\']*\')', _mask_quote, clean_text)

    # Perform split
    parts = _CONJUNCTION_PATTERN.split(masked)
    if len(parts) <= 1:
        return [clean_text]

    # Unmask and clean parts
    results: List[str] = []
    for part in parts:
        unmasked = part
        for idx, q in enumerate(quotes):
            unmasked = unmasked.replace(f"__QUOTE_{idx}__", q)
        cleaned_sub = unmasked.strip(" ,;.")
        if cleaned_sub:
            results.append(cleaned_sub)

    # If splitting produced fewer than 2 valid non-empty parts, return original
    if len(results) < 2:
        return [clean_text]

    return results
