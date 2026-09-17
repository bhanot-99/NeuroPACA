"""
Category H — Communication.
Exposes:
  - read_latest_emails / search_emails:
      * List recent emails with sender, subject, date, and 100-char snippet.
      * Filter by sender (e.g. "any email from swatik").
      * Filter by query/keyword (e.g. "search emails for IPO").
      * Read specific email body by index (e.g. "read 2nd recent email", "read email 3").
  - send_email:
      * Send email with recipient, subject, and body via live SMTP.
"""

import re
from collections.abc import Callable

_ORDINALS: dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7,
    "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
    "latest": 1, "newest": 1, "last": 1,
}


def _parse_ordinal(word: str | None) -> int | None:
    if not word:
        return None
    w = word.lower().strip()
    if w in _ORDINALS:
        return _ORDINALS[w]
    if w.isdigit():
        return int(w)
    m = re.match(r"^(\d+)(?:st|nd|rd|th)?$", w)
    if m:
        return int(m.group(1))
    return None


def _match_read_latest_emails(text: str) -> dict | None:
    """Matches email reading and search utterances, extracting count, sender, query, and index."""
    # 1. Specific index on singular email/message (full body requested):
    # e.g. "read 2nd recent email", "read the second email", "read the first email from swatik"
    m_idx = re.search(
        r"\bread\s+(?:the\s+)?(\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|latest|newest|last)\s+(?:recent\s+)?(?:e-?mail|message)(?:\s+from\s+([^\s,]+))?\b",
        text, re.IGNORECASE
    )
    if m_idx:
        idx = _parse_ordinal(m_idx.group(1))
        sender = m_idx.group(2).strip() if m_idx.group(2) else None
        return {"index": idx, "sender": sender, "full_body": True}

    # e.g. "read email 3", "read email number 2 from groww"
    m_idx2 = re.search(
        r"\bread\s+(?:the\s+)?(?:e-?mail|message)\s+(?:number\s+|#\s*)?(\d+)(?:\s+from\s+([^\s,]+))?\b",
        text, re.IGNORECASE
    )
    if m_idx2:
        idx = int(m_idx2.group(1))
        sender = m_idx2.group(2).strip() if m_idx2.group(2) else None
        return {"index": idx, "sender": sender, "full_body": True}

    # 2. Specific sender query: e.g. "any email from swatik", "check emails from groww", "emails from boss"
    m_sender = re.search(
        r"\b(?:any|check|read|get|find|show|search)\s+(?:my\s+)?(?:e-?mails?|messages?)\s+from\s+([^\s,]+)\b"
        r"|\b(?:did\s+I\s+get\s+(?:an\s+)?e-?mail\s+from\s+([^\s,]+))\b"
        r"|\be-?mails?\s+from\s+([^\s,]+)\b",
        text, re.IGNORECASE
    )
    if m_sender:
        sender = next((g for g in m_sender.groups() if g), "").strip()
        return {"sender": sender, "count": 5}

    # 3. Query search: e.g. "search emails for IPO", "find emails about invoice"
    m_query = re.search(
        r"\b(?:search|find)\s+(?:my\s+)?e-?mails?\s+(?:for|about|with\s+query)\s+[\"']?([^\"']+)[\"']?",
        text, re.IGNORECASE
    )
    if m_query:
        query = m_query.group(1).strip()
        return {"query": query, "count": 5}

    # 4. General list with optional count: "check my latest emails", "read my last 3 emails", "show 10 emails"
    m_list = re.search(
        r"\b(?:check|read|get|list|show)\s+(?:my\s+)?(?:(?:latest|new|recent|unread|last)\s+)?(?:(\d+)\s+)?(?:e-?mails?|messages?)\b"
        r"|\b(?:any\s+new\s+e-?mails?)\b"
        r"|\bwhat(?:'s|\s+are)\s+my\s+(?:latest\s+|recent\s+)?e-?mails?\b",
        text, re.IGNORECASE
    )
    if m_list:
        cnt = int(m_list.group(1)) if m_list.group(1) else 5
        return {"count": cnt}

    return None


def _match_send_email(text: str) -> dict | None:
    """Matches email send commands, extracting recipient, subject, and body."""
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
    ("read_latest_emails", _match_read_latest_emails),
    ("search_emails", _match_read_latest_emails),
    ("send_email", _match_send_email),
]
