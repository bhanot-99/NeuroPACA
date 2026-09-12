# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S1 · email threading and participant normalization (VISION_PHASES.md).

Deterministic Message-ID / In-Reply-To / References threading that correctly
groups multi-message correspondence threads regardless of message arrival order.
"""

from __future__ import annotations

import email.utils
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any


def normalize_message_id(mid: str | None) -> str:
    """Strip angle brackets, whitespace, and normalize a Message-ID."""
    if not mid:
        return ""
    cleaned = mid.strip().strip("<>").strip()
    return cleaned


def extract_message_ids(header_val: str | Sequence[str] | None) -> list[str]:
    """Parse a header containing one or more Message-IDs (e.g. References)."""
    if not header_val:
        return []
    if isinstance(header_val, str):
        # Extract everything matching <...> or whitespace-separated tokens
        matches = re.findall(r"<([^>]+)>", header_val)
        if matches:
            return [normalize_message_id(m) for m in matches if m.strip()]
        return [normalize_message_id(tok) for tok in header_val.split() if tok.strip()]
    out: list[str] = []
    for item in header_val:
        out.extend(extract_message_ids(item))
    return out


def normalize_email_address(addr_str: str) -> tuple[str, str]:
    """Parse 'Display Name <user@domain.com>' into (display_name, email_address)."""
    if not addr_str:
        return "", ""
    name, addr = email.utils.parseaddr(addr_str)
    addr = addr.strip().lower()
    name = name.strip()
    if "@" not in addr and "<" not in addr_str:
        return addr_str.strip(), ""
    if not name and addr:
        name = addr.split("@")[0].replace(".", " ").title()
    return name, addr


def person_slug(name: str, email_addr: str) -> str:
    """Deterministic slug for person:<slug> in GraphMemory and EpisodeStore.

    Derived address-first so that distinct email addresses with identical display
    names (e.g. two contacts named 'Maya' or 'Support') never collide.
    """
    raw_email = email_addr.strip().lower()
    if raw_email:
        cleaned = re.sub(r"[^a-z0-9]+", "-", raw_email).strip("-")
        if cleaned:
            return cleaned

    raw_name = name.strip()
    if raw_name:
        cleaned = re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-")
        if cleaned:
            return cleaned

    return "unknown"


def thread_id_from_msg(msg_id: str) -> str:
    """Sanitize root Message-ID into a clean thread ID for thread:<id>."""
    cleaned = normalize_message_id(msg_id)
    sanitized = re.sub(r"[^a-zA-Z0-9._\-@]", "", cleaned)
    return sanitized or "unknown-thread"


def clean_subject(subject: str) -> str:
    """Strip leading Re:, Fwd:, Fw:, etc. repeatedly."""
    return re.sub(r"^((re|fwd|fw)\s*:\s*)+", "", subject.strip(), flags=re.IGNORECASE).strip()


class Threader:
    """Stateful email thread grouper.

    Resolves incoming messages to their canonical root thread ID based on:
    1. References (root is references[0])
    2. In-Reply-To (parent message)
    3. Subject matching as a fallback for missing headers
    """

    def __init__(self) -> None:
        self._msg_to_root: dict[str, str] = {}
        self._subject_to_root: dict[str, tuple[str, datetime | None]] = {}

    def get_thread_id(
        self,
        message_id: str,
        in_reply_to: str | None = None,
        references: Sequence[str] | None = None,
        subject: str = "",
        date: datetime | None = None,
    ) -> str:
        """Return the canonical thread ID (clean slug) for a message."""
        mid = normalize_message_id(message_id)
        irt = normalize_message_id(in_reply_to) if in_reply_to else ""
        refs = [normalize_message_id(r) for r in (references or []) if r]

        # 1. Check if references contains an ancestor
        root: str | None = None
        if refs:
            first_ref = refs[0]
            # Resolve any existing mapping
            root = self._msg_to_root.get(first_ref, first_ref)
            # Link all references to this root
            for ref in refs:
                self._msg_to_root[ref] = root
        elif irt:
            root = self._msg_to_root.get(irt, irt)
            self._msg_to_root[irt] = root
        elif mid in self._msg_to_root:
            root = self._msg_to_root[mid]

        # Fallback to subject if no headers linked to an existing thread
        subj_clean = clean_subject(subject).lower()
        if (root is None or root == mid) and subj_clean:
            if subj_clean in self._subject_to_root:
                sub_root, sub_dt = self._subject_to_root[subj_clean]
                if (
                    date is None
                    or sub_dt is None
                    or abs((date - sub_dt).total_seconds()) < 86400 * 30
                ):
                    root = sub_root
            else:
                self._subject_to_root[subj_clean] = (mid or root or "thread", date)

        if not root:
            root = mid or "thread"

        if mid:
            self._msg_to_root[mid] = root
        if irt and irt not in self._msg_to_root:
            self._msg_to_root[irt] = root

        return thread_id_from_msg(root)

    def to_dict(self) -> dict[str, Any]:
        """Serialize threader state to a JSON-compatible dict."""
        return {
            "msg_to_root": dict(self._msg_to_root),
            "subject_to_root": {
                k: [root, dt.isoformat() if dt else None]
                for k, (root, dt) in self._subject_to_root.items()
            },
        }

    def load_dict(self, data: dict[str, Any], cutoff_date: datetime | None = None) -> None:
        """Load state from dict, pruning subject entries older than cutoff_date."""
        if not isinstance(data, dict):
            return
        msg_to_root = data.get("msg_to_root", {})
        if isinstance(msg_to_root, dict):
            if len(msg_to_root) > 10000:
                self._msg_to_root = dict(list(msg_to_root.items())[-10000:])
            else:
                self._msg_to_root = {str(k): str(v) for k, v in msg_to_root.items()}

        subject_to_root = data.get("subject_to_root", {})
        if isinstance(subject_to_root, dict):
            for k, val in subject_to_root.items():
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    root, dt_str = val[0], val[1]
                    dt = None
                    if dt_str:
                        try:
                            dt = datetime.fromisoformat(dt_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                        except Exception:
                            dt = None
                    if cutoff_date and dt and dt < cutoff_date:
                        continue
                    self._subject_to_root[str(k)] = (str(root), dt)
            if len(self._subject_to_root) > 10000:
                self._subject_to_root = dict(list(self._subject_to_root.items())[-10000:])

    def prune(self, now: datetime, max_age_seconds: float = 86400 * 30) -> None:
        """Prune subject fallback entries older than max_age_seconds and bound table sizes."""
        cutoff = now - timedelta(seconds=max_age_seconds)
        pruned_subject: dict[str, tuple[str, datetime | None]] = {}
        for k, (root, dt) in self._subject_to_root.items():
            if dt is None or dt >= cutoff:
                pruned_subject[k] = (root, dt)
        if len(pruned_subject) > 10000:
            pruned_subject = dict(list(pruned_subject.items())[-10000:])
        self._subject_to_root = pruned_subject

        if len(self._msg_to_root) > 10000:
            self._msg_to_root = dict(list(self._msg_to_root.items())[-10000:])


# gen-ref: 4a009bbe
