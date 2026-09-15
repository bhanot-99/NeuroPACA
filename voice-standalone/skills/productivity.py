"""
Category G — Productivity & reminders. Per explicit scope decision, only a
minimal test to-do feature is built here (add + list), NOT the full 16-item
catalog — alarms/timers need real background scheduling, notes/clipboard/
meal-list need their own design, none of that is in scope for this pass.

  G_add   add_todo   — "add a to-do: buy milk", "remind me to call mom"
  G_list  list_todos — "list my to-dos", "what's on my to-do list"

Storage: a flat JSON file, not a database — this is a test feature, and a
single list of strings needs nothing heavier.
"""

import re
from collections.abc import Callable


def _match_add_todo(text: str) -> dict | None:
    m = re.search(
        r"\badd\s+(?:a\s+)?to[\s-]?do\s*[:,]?\s*(.+)"
        r"|\bremind\s+me\s+to\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    item = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    return {"item": item} if item else None


def _match_list_todos(text: str) -> dict | None:
    if re.search(r"\blist\s+(?:my\s+)?to[\s-]?dos?\b"
                 r"|\bwhat(?:'s|\s+is)\s+on\s+my\s+to[\s-]?do\s+list\b",
                 text, re.IGNORECASE):
        return {}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("add_todo", _match_add_todo),
    ("list_todos", _match_list_todos),
]
