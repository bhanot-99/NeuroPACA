"""
Category C1 / Search — Explicit Web Search Triggers.
Exposes:
  - google_search:
      * Only matches explicit search keywords:
        - "search for <query>"
        - "google <query>"
        - "search the web / internet / online for <query>"
        - "look up <query> on google"
      * Strictly excludes broad catch-alls or unanchored text.
"""

import re
from collections.abc import Callable


def _match_google_search(text: str) -> dict | None:
    """Matches only explicit web search triggers:
    - 'search for <query>'
    - 'google <query>'
    - 'search the web / internet / online for <query>'
    - 'look up <query> on google'
    Does NOT match broad 'search <query>' or unanchored text."""
    clean = text.strip()
    m = re.search(
        r"^\s*google\s+(.+)"
        r"|\bgoogle\s+(?:search\s+(?:for\s+)?|for\s+)?(.+)"
        r"|\bsearch\s+for\s+(.+)"
        r"|\bsearch\s+(?:the\s+)?(?:web|internet|online)\s+(?:for\s+)?(.+)"
        r"|\blook\s+up\s+(.+?)\s+on\s+google\b",
        clean,
        re.IGNORECASE,
    )
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().rstrip(".")
    if not query:
        return None
    return {"query": query}


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("google_search", _match_google_search),
]
