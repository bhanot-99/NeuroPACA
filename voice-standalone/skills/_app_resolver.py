"""
Shared correction-layer helper: resolve a spoken app name to a real desktop ID.

Strategy (mirrors ARCHITECTURE.md's correction-layer design):
  1. Substring match first (instant, zero false-negatives for unambiguous names).
  2. Fuzzy match second, but only accept high-confidence hits (cutoff 0.75)
     to avoid silently launching the wrong app when nothing close is installed.
  3. Return None rather than guess — the caller decides what to do (fall through
     to LLM, ask for clarification, etc.).
"""

import difflib
import glob
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _installed_apps() -> list[tuple[str, str]]:
    """Return (desktop_id, display_name) pairs from all .desktop files.

    Result is cached for the process lifetime — app installs during a session
    are rare enough that a restart is acceptable to pick them up.
    """
    apps: list[tuple[str, str]] = []
    search_dirs = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        os.path.expanduser("~/.local/share/applications"),
    ]
    for base in search_dirs:
        for path in glob.glob(os.path.join(base, "*.desktop")):
            desktop_id = os.path.splitext(os.path.basename(path))[0]
            display_name = desktop_id
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("Name="):
                            display_name = line.removeprefix("Name=").strip()
                            break
            except OSError:
                pass
            apps.append((desktop_id, display_name))
    return apps


def resolve_app_name(spoken: str) -> str | None:
    """Map a spoken app name to a desktop_id, or return None if unresolved.

    Args:
        spoken: lowercased, stripped transcription of the app name token.

    Returns:
        A desktop_id string (e.g. "code", "com.system76.CosmicFiles") if a
        confident match exists, or None if nothing close enough is installed.
    """
    if not spoken:
        return None

    apps = _installed_apps()

    # Pass 1: substring containment — confident zero-ambiguity matches.
    # "code" → "Visual Studio Code" ✓; "fire" → "Firefox" ✓
    for desktop_id, display_name in apps:
        if spoken in display_name.lower() or spoken in desktop_id.lower():
            return desktop_id

    # Pass 2: fuzzy match against display names only, high cutoff.
    # Handles transcription mishearings (e.g. "fire fox" → "Firefox").
    # cutoff=0.75 means we need a close match; lower would produce false positives.
    name_to_id = {display_name.lower(): desktop_id for desktop_id, display_name in apps}
    close = difflib.get_close_matches(spoken, name_to_id.keys(), n=1, cutoff=0.75)
    if close:
        return name_to_id[close[0]]

    return None
