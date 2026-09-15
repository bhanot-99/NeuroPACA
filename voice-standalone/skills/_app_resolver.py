"""
App-specific wrapper around the generalized correction layer (Step 3):
resolve a spoken app name to a real desktop ID.

The actual matching algorithm (substring first, then the edit-distance/
token-overlap ensemble, threshold-gated) lives in skills/_correction.py —
this module's only job is supplying the installed-apps candidate list.
"""

import glob
import os
from functools import lru_cache

from skills._correction import resolve_against_known


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
    # Each app contributes two candidates so both its display name ("Visual
    # Studio Code") and its raw desktop_id ("code") are checked — matches the
    # original two-field behavior, now routed through the shared ensemble.
    candidates: list[tuple[str, str]] = []
    for desktop_id, display_name in apps:
        candidates.append((display_name.lower(), desktop_id))
        candidates.append((desktop_id.lower(), desktop_id))

    return resolve_against_known(spoken, candidates, cutoff=0.75)
