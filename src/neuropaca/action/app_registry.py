# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.2 · `app_registry` — resolution of app names against installed software (VISION_PHASES.md).

Tier 2 of the voice command pipeline:
Reads `.desktop` files from freedesktop standard directories (/usr/share/applications,
~/.local/share/applications) to find installed applications, and resolves target
names using `difflib.SequenceMatcher` (stdlib, zero new dependencies).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from pathlib import Path

# Standard freedesktop application directories
_STANDARD_APP_DIRS = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
)

_FIELD_CODE_RE = re.compile(r"%[a-zA-Z]")


def _parse_desktop_file(path: Path) -> tuple[str, str] | None:
    """Parse a single .desktop file for [Desktop Entry] Name and Exec.

    Skips entries with NoDisplay=true, Hidden=true, or Type != Application.
    Strips freedesktop field codes (%u, %F, etc.) from the Exec line.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    in_entry = False
    name = ""
    exec_cmd = ""
    nodisplay = False
    hidden = False
    app_type = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry:
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "Name" and not name:
            name = val
        elif key == "Exec" and not exec_cmd:
            exec_cmd = val
        elif key == "NoDisplay" and val.lower() == "true":
            nodisplay = True
        elif key == "Hidden" and val.lower() == "true":
            hidden = True
        elif key == "Type":
            app_type = val

    if nodisplay or hidden:
        return None
    if app_type and app_type != "Application":
        return None
    if not name or not exec_cmd:
        return None

    clean_exec = _FIELD_CODE_RE.sub("", exec_cmd).strip()
    return name, clean_exec


def list_installed_apps(
    search_dirs: Sequence[Path | str] | None = None,
) -> dict[str, str]:
    """Scan .desktop files from standard locations (or search_dirs if provided).

    Returns `{display name: launch command}`.
    """
    dirs = (
        [Path(d).expanduser() for d in search_dirs]
        if search_dirs is not None
        else list(_STANDARD_APP_DIRS)
    )
    apps: dict[str, str] = {}
    for app_dir in dirs:
        if not app_dir.is_dir():
            continue
        try:
            entries = sorted(app_dir.glob("*.desktop"))
        except OSError:
            continue
        for entry in entries:
            parsed = _parse_desktop_file(entry)
            if parsed is not None:
                name, cmd = parsed
                if name not in apps:
                    apps[name] = cmd
    return apps


def _score_app_name(query: str, target: str) -> float:
    """Score query against target app name using SequenceMatcher + word matches."""
    q = query.strip().lower()
    t = target.strip().lower()
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0

    full_ratio = difflib.SequenceMatcher(None, q, t).ratio()
    words = t.split()
    word_ratios = [difflib.SequenceMatcher(None, q, w).ratio() for w in words]
    best_word = max(word_ratios) if word_ratios else 0.0

    # Strong match if query is an exact word in target ("brave" -> "Brave Browser")
    if any(q == w for w in words):
        return max(full_ratio, 0.9)
    # Prefix match for words with length >= 3 ("calc" -> "Calculator")
    if any(w.startswith(q) for w in words) and len(q) >= 3:
        return max(full_ratio, 0.8)

    return max(full_ratio, best_word)


def resolve_app_name(
    query: str,
    apps: dict[str, str],
    threshold: float = 0.6,
) -> list[tuple[str, float]]:
    """Score query against every known app name in apps.

    Returns matches at or above threshold, sorted best first.
    """
    q = query.strip()
    if not q:
        return []

    results: list[tuple[str, float]] = []
    for app_name in apps:
        score = _score_app_name(q, app_name)
        if score >= threshold:
            results.append((app_name, score))

    # Sort descending by score, then ascending by name for determinism
    results.sort(key=lambda item: (-item[1], item[0]))
    return results
