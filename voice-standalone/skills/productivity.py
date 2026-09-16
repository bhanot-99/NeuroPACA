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


def _match_read_latest_emails(text: str) -> dict | None:
    if re.search(
        r"\b(?:check|read|get|list|show)\s+(?:my\s+)?(?:latest\s+|new\s+|recent\s+|unread\s+)?(?:e-?mails?|messages?)\b"
        r"|\b(?:any\s+new\s+e-?mails?)\b"
        r"|\bwhat(?:'s|\s+are)\s+my\s+(?:latest\s+|recent\s+)?e-?mails?\b",
        text, re.IGNORECASE
    ):
        return {"count": 5}
    return None


def _match_send_email(text: str) -> dict | None:
    m = re.search(r"\b(?:send\s+(?:an\s+)?e-?mail|e-?mail)\s+to\s+([^\s,:]+@[^\s,:]+)(.*)", text, re.IGNORECASE)
    if not m:
        return None
    to_addr = m.group(1).strip()
    rest = m.group(2).strip()
    subject = "Voice Assistant Message"
    body = "Hello from NeuroPaca Voice Assistant."

    sub_m = re.search(r"\b(?:with\s+)?subject\s+[\"']?(.*?)(?:[\"']?\s+(?:and\s+)?(?:message|body|saying)\b|$)", rest, re.IGNORECASE)
    if sub_m and sub_m.group(1).strip():
        subject = sub_m.group(1).strip().strip("\"'")

    body_m = re.search(r"\b(?:and\s+)?(?:message|body|saying)\s+[\"']?(.*)", rest, re.IGNORECASE)
    if body_m and body_m.group(1).strip():
        body = body_m.group(1).strip().strip("\"'")

    return {"to": to_addr, "subject": subject, "body": body}


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("add_todo", _match_add_todo),
    ("list_todos", _match_list_todos),
    ("read_latest_emails", _match_read_latest_emails),
    ("send_email", _match_send_email),
]

