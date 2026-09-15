"""
Shared spoken-folder-name resolution for Category E (files) skills.

Maps common spoken folder names ("my downloads", "the desktop") to real XDG
user directories. Anything not recognized is treated as a literal path
fragment under $HOME, resolved by the caller.
"""

import os

_NAMED_FOLDERS = {
    "downloads": "~/Downloads",
    "download": "~/Downloads",
    "documents": "~/Documents",
    "document": "~/Documents",
    "desktop": "~/Desktop",
    "pictures": "~/Pictures",
    "photos": "~/Pictures",
    "music": "~/Music",
    "videos": "~/Videos",
    "movies": "~/Videos",
    "home": "~",
    "trash": "~/.local/share/Trash/files",
}


def resolve_folder(spoken: str) -> str | None:
    """Resolve a spoken folder reference to a real, existing directory path.

    Returns None if it doesn't resolve to an existing directory — callers
    decide what to do next (fall through, error message, etc.), never guess.
    """
    key = spoken.strip().lower()
    if key in _NAMED_FOLDERS:
        path = os.path.expanduser(_NAMED_FOLDERS[key])
        return path if os.path.isdir(path) else None

    # Literal path under $HOME, or an absolute/relative path as typed.
    candidate = os.path.expanduser(spoken.strip())
    if os.path.isdir(candidate):
        return candidate
    candidate2 = os.path.expanduser(os.path.join("~", spoken.strip()))
    if os.path.isdir(candidate2):
        return candidate2
    return None


_SEARCH_DIRS = ["~/Downloads", "~/Documents", "~/Desktop", "~/Pictures", "~/Music", "~/Videos", "~"]


def find_file(name: str) -> str | None:
    """Find a file by name (exact or literal path), searching common
    directories only (not a full filesystem crawl — that's slow and mostly
    irrelevant to where a user's own files actually live). Returns the first
    match found, or None. Top-level of each directory only, not recursive —
    keeps this fast and predictable rather than an unbounded search."""
    literal = os.path.expanduser(name.strip())
    if os.path.isfile(literal):
        return literal

    for base in _SEARCH_DIRS:
        candidate = os.path.expanduser(os.path.join(base, name.strip()))
        if os.path.isfile(candidate):
            return candidate

    for base in _SEARCH_DIRS:
        base_path = os.path.expanduser(base)
        if not os.path.isdir(base_path):
            continue
        try:
            for entry in os.listdir(base_path):
                if entry.lower() == name.strip().lower():
                    full = os.path.join(base_path, entry)
                    if os.path.isfile(full):
                        return full
        except OSError:
            continue
    return None
