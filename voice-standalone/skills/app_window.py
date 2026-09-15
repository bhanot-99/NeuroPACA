"""
Category B — App & window management (10 skills).

COSMIC / Wayland research findings (researched against the live system before
writing any executor — see actions.py for implementation decisions):

  WHAT WORKS ON COSMIC:
  - gtk-launch <desktop-id>          → open app (existing, proven in Phase 0)
  - cosmic-launcher                  → open the COSMIC app launcher UI
  - DBUS: com.system76.CosmicSession  Exit()/Restart() → log out / restart session
  - DBUS: com.system76.CosmicComp    → only exposes Ei (emulated input), no window API
  - No COSMIC D-Bus interface for: close window, switch-to window, list open windows,
    switch workspace, maximize, minimize-all, force-quit.

  WHY X11 TOOLS DON'T WORK:
  - cosmic-comp is a native Wayland compositor; it provides no XWayland bridge for
    wmctrl/xdotool window enumeration (those tools return empty or error).
  - There are no documented compositor protocols (e.g. wlr-foreign-toplevel) exposed
    by cosmic-comp that could be used from Python today.

  PER-SKILL DECISIONS (see executors in actions.py for the comment blocks):
  B01 open_app             ✓  gtk-launch (proven in Phase 0)
  B02 close_app            ✗  No COSMIC D-Bus / Wayland protocol available
  B03 switch_to_app        ✗  Same: no window-focus API on COSMIC Wayland
  B04 list_open_apps       ✗  Same: cannot enumerate windows without compositor API
  B05 switch_workspace     ✗  No D-Bus API on cosmic-comp today
  B06 show_desktop         ✗  No minimize-all API on COSMIC today
  B07 maximize_window      ✗  No window-state API on COSMIC today
  B08 force_quit           ✗  Can kill by process name via `pkill` if app name resolves —
                               implemented with subprocess pkill; uses app-name resolver
  B09 open_launcher        ✓  `cosmic-launcher` binary opens the app launcher
  B10 reopen_last_closed   ✗  No COSMIC API; would require our own session tracking
                               (not in scope for Step 1)

Skills B02, B03, B04, B05, B06, B07, B10: matchers are implemented and return kwargs
on a confident hit so the pipeline is honest about what it understood — the executor
logs the limitation and exits cleanly, enabling the LLM fallback to try if needed.
"""

import re
from collections.abc import Callable

from skills._app_resolver import resolve_app_name


# ---------------------------------------------------------------------------
# B01 — open_app
# ---------------------------------------------------------------------------

def _match_open_app(text: str) -> dict | None:
    m = re.search(r"\b(?:open|launch|start)\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    spoken = m.group(1).strip().rstrip(".").lower()
    # Strip trailing noise words
    spoken = re.sub(r"\s+(?:app|application|program)$", "", spoken).strip()
    if not spoken:
        return None
    desktop_id = resolve_app_name(spoken)
    if desktop_id is None:
        return None
    return {"app_name": desktop_id}


# ---------------------------------------------------------------------------
# B02 — close_app
# (Matcher fires; executor is a documented no-op — see actions.py B02 comment)
# ---------------------------------------------------------------------------

def _match_close_app(text: str) -> dict | None:
    # Exclude "force quit" / "force close" / "kill" — those belong to force_quit (B08)
    if re.search(r"\bforce\s+(?:quit|close|kill)\b|\bkill\b|\bpkill\b", text, re.IGNORECASE):
        return None
    m = re.search(
        r"\b(?:close|quit|exit)\s+(.+?)(?:\s+(?:app|application|window|program))?\s*$",
        text, re.IGNORECASE
    )
    if not m:
        return None
    spoken = m.group(1).strip().rstrip(".").lower()
    # Don't catch "quit everything" / "close all" — too ambiguous, let LLM handle
    if re.search(r"\b(?:all|everything|all\s+windows)\b", spoken):
        return None
    desktop_id = resolve_app_name(spoken)
    if desktop_id is None:
        return None
    return {"app_name": desktop_id}


# ---------------------------------------------------------------------------
# B03 — switch_to_app
# (Matcher fires; executor is a documented no-op — see actions.py B03 comment)
# ---------------------------------------------------------------------------

def _match_switch_to_app(text: str) -> dict | None:
    m = re.search(
        r"\b(?:switch\s+to|go\s+to|focus|bring\s+up)\s+(.+?)(?:\s+(?:app|application|window))?\s*$",
        text, re.IGNORECASE
    )
    if not m:
        return None
    spoken = m.group(1).strip().rstrip(".").lower()
    desktop_id = resolve_app_name(spoken)
    if desktop_id is None:
        return None
    return {"app_name": desktop_id}


# ---------------------------------------------------------------------------
# B04 — list_open_apps
# (Matcher fires; executor is a documented no-op — see actions.py B04 comment)
# ---------------------------------------------------------------------------

def _match_list_open_apps(text: str) -> dict | None:
    if re.search(
        r"\b(?:list|show|what(?:'s|\s+is|\s+are))\s+(?:all\s+)?(?:open|running)\s+(?:apps?|applications?|windows?|programs?)\b"
        r"|\bwhat(?:'s|\s+is)\s+(?:open|running)\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# B05 — switch_workspace
# (Matcher fires; executor is a documented no-op — see actions.py B05 comment)
# ---------------------------------------------------------------------------

def _match_switch_workspace(text: str) -> dict | None:
    m = re.search(
        r"\b(?:switch\s+to|go\s+to|move\s+to)\s+workspace\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b"
        r"|\bworkspace\s+(\d+)\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    raw = (m.group(1) or m.group(2)).lower()
    word_map = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    num = word_map.get(raw, int(raw) if raw.isdigit() else None)
    if num is None:
        return None
    return {"number": num}


# ---------------------------------------------------------------------------
# B06 — show_desktop
# (Matcher fires; executor is a documented no-op — see actions.py B06 comment)
# ---------------------------------------------------------------------------

def _match_show_desktop(text: str) -> dict | None:
    if re.search(
        r"\bshow\s+(?:the\s+)?desktop\b"
        r"|\bminimize\s+all\s+(?:windows|apps|applications)?\b"
        r"|\bhide\s+all\s+(?:windows|apps|applications)\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# B07 — maximize_window
# (Matcher fires; executor is a documented no-op — see actions.py B07 comment)
# ---------------------------------------------------------------------------

def _match_maximize_window(text: str) -> dict | None:
    if re.search(
        r"\bmaximize\s+(?:the\s+)?(?:current\s+)?(?:window|app|application)?\b"
        r"|\bfull(?:screen)?\s+(?:the\s+)?(?:current\s+)?(?:window|app|application)?\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# B08 — force_quit
# Uses pkill by process name — works on any POSIX system including COSMIC.
# The app-name resolver maps spoken name → desktop_id; we derive a kill target
# from the desktop_id basename (e.g. "code" → pkill -i code).  🔒
# ---------------------------------------------------------------------------

def _match_force_quit(text: str) -> dict | None:
    m = re.search(
        r"\b(?:force\s+quit|force\s+close|force\s+kill|kill|pkill)\s+(.+?)(?:\s+(?:app|application|process|program))?\s*$",
        text, re.IGNORECASE
    )
    if not m:
        return None
    spoken = m.group(1).strip().rstrip(".").lower()
    desktop_id = resolve_app_name(spoken)
    if desktop_id is None:
        return None
    return {"app_name": desktop_id}


# ---------------------------------------------------------------------------
# B09 — open_launcher
# ---------------------------------------------------------------------------

def _match_open_launcher(text: str) -> dict | None:
    if re.search(
        r"\b(?:open|show|launch|toggle)?\s*(?:app\s+)?launcher\b"
        r"|\bopen\s+app\s+library\b"
        r"|\bshow\s+(?:all\s+)?apps?\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# B10 — reopen_last_closed
# (Matcher fires; executor is a documented no-op — see actions.py B10 comment)
# ---------------------------------------------------------------------------

def _match_reopen_last_closed(text: str) -> dict | None:
    if re.search(
        r"\breopen\s+(?:last|previous)\s+(?:closed\s+)?(?:app|application|window)\b"
        r"|\bundo\s+(?:close|quit)\b"
        r"|\brestore\s+(?:last|previous)\s+(?:closed\s+)?(?:app|window)\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# Ordered skill registry.
# open_app is LAST in this category: its broad "open|launch|start .+" pattern
# would swallow more specific skills if it ran first.
# force_quit before open_app for the same reason (both consume an app name).
# list/show/switch patterns are distinctive enough to run in any order.
# ---------------------------------------------------------------------------

SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("switch_workspace", _match_switch_workspace),
    ("switch_to_app", _match_switch_to_app),
    ("close_app", _match_close_app),
    ("force_quit", _match_force_quit),
    ("list_open_apps", _match_list_open_apps),
    ("show_desktop", _match_show_desktop),
    ("maximize_window", _match_maximize_window),
    ("reopen_last_closed", _match_reopen_last_closed),
    ("open_launcher", _match_open_launcher),
    ("open_app", _match_open_app),   # ← broadest pattern, always last
]
