"""
_compound_splitter.py — Deterministic Compound Command Pre-Processor.

Splits multi-action user utterances on conjunctions (" and ", ", and ", " then ", "; ")
into sequential atomic sub-commands, while preserving conjunctions that are internal
to single commands (such as search queries, email bodies, to-do items, or quoted strings).
"""

import re
from typing import List

# Conjunction split regex: splits on semicolons, ", and ", " and ", ", then ", " then ", ", and then ", " and then "
_CONJUNCTION_PATTERN = re.compile(
    r"(?:;\s*|,\s*(?:and\s+then|and|then)\s+|\s+(?:and\s+then|and|then)\s+(?!and\b))",
    re.IGNORECASE,
)

# Known action/command verbs that force a split even within an atomic prefix guard
ACTION_VERBS = r"\b(send|set|read|open|lock|turn|show|calculate|list|create|delete|run|launch|fetch|check)\b"

_ACTION_VERB_REGEX = re.compile(
    r"^\s*(?:(?:then|please|kindly)\s+)*" + ACTION_VERBS,
    re.IGNORECASE,
)

# Atomic command prefixes where "and"/"then" is treated as part of the query/payload, not a command delimiter
_ATOMIC_PREFIX_REGEX = re.compile(
    r"^\s*(?:"
    r"search\s+for\s+|"
    r"search\s+the\s+web\s+for\s+|"
    r"find\s+|"
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
    r"spell\s+|"
    r"hello\s+|"
    r"hi\s+|"
    r"hey\s+|"
    r"greetings\s+|"
    r"good\s+(?:morning|afternoon|evening)\s+|"
    r"my\s+name\s+is\s+|"
    r"how\s+are\s+you"
    r")",
    re.IGNORECASE,
)


def _has_atomic_prefix(text: str) -> bool:
    """Returns True if the text begins with an atomic prefix or email command syntax."""
    trimmed = text.strip()
    if _ATOMIC_PREFIX_REGEX.match(trimmed):
        return True
    # Email syntax check: "send ... to ... with subject ... and message/body ..."
    if "send" in trimmed.lower() and "email" in trimmed.lower() and "@" in trimmed:
        return True
    return False


def _is_command_continuation(text: str) -> bool:
    """Returns True if the text following a conjunction starts with an action verb or atomic command prefix."""
    trimmed = text.strip()
    return bool(_ACTION_VERB_REGEX.match(trimmed) or _ATOMIC_PREFIX_REGEX.match(trimmed))


def _is_atomic_command(text: str) -> bool:
    """Returns True if the utterance is an atomic skill that should not be split."""
    trimmed = text.strip()
    if not _has_atomic_prefix(trimmed):
        return False
    # If the command begins with an atomic prefix, check if it contains a
    # conjunction followed by an action verb (which indicates a compound command).
    for m in _CONJUNCTION_PATTERN.finditer(trimmed):
        if ";" in m.group(0):
            return False
        post_text = trimmed[m.end():].strip()
        if _is_command_continuation(post_text):
            return False
    return True


def split_compound_utterance(text: str) -> List[str]:
    """Splits a compound command into atomic sub-commands.
    
    Examples:
        'set volume to 90% and brightness to 90%' -> ['set volume to 90%', 'brightness to 90%']
        'turn off wifi then lock screen' -> ['turn off wifi', 'lock screen']
        'battery status; current time' -> ['battery status', 'current time']
        'search for tom and jerry' -> ['search for tom and jerry']
        'Search for Tom and Jerry and send an email to Alice' -> ['Search for Tom and Jerry', 'send an email to Alice']
    """
    if not text or not text.strip():
        return []

    clean_text = text.strip()

    # If the entire utterance is atomic (with no secondary action verbs), preserve whole
    if _is_atomic_command(clean_text):
        return [clean_text]

    # Mask quoted strings to prevent splitting inside quotes
    quotes: List[str] = []
    def _mask_quote(m):
        quotes.append(m.group(0))
        return f"__QUOTE_{len(quotes)-1}__"

    masked = re.sub(r'("[^"]*"|\'[^\']*\')', _mask_quote, clean_text)

    matches = list(_CONJUNCTION_PATTERN.finditer(masked))
    if not matches:
        return [clean_text]

    segments: List[str] = []
    current_start = 0

    for m in matches:
        if m.start() < current_start:
            continue

        left_chunk = masked[current_start:m.start()].strip()
        if not left_chunk:
            continue

        conj = m.group(0)
        post_text = masked[m.end():].strip()

        # Semicolons always split
        if ";" in conj:
            segments.append(left_chunk)
            current_start = m.end()
            continue

        # If left_chunk has an atomic prefix guard, only split if followed by an action verb or command
        if _has_atomic_prefix(left_chunk):
            if _is_command_continuation(post_text):
                segments.append(left_chunk)
                current_start = m.end()
            else:
                # Suppress split: conjunction is part of the atomic query/payload (e.g. "Tom and Jerry")
                continue
        else:
            # Normal command: split on conjunction
            segments.append(left_chunk)
            current_start = m.end()

    # Trailing segment
    tail = masked[current_start:].strip()
    if tail:
        segments.append(tail)

    # Unmask and clean parts
    results: List[str] = []
    for segment in segments:
        unmasked = segment
        for idx, q in enumerate(quotes):
            unmasked = unmasked.replace(f"__QUOTE_{idx}__", q)
        cleaned_sub = unmasked.strip(" ,;.")
        if cleaned_sub:
            results.append(cleaned_sub)

    # If splitting produced fewer than 2 valid non-empty parts, return original
    if len(results) < 2:
        return [clean_text]

    return results
