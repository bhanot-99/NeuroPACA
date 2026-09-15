"""
Category D — Media & entertainment playback (13 skills, Step 4; Spotify is
⚙️-tagged and deferred).

  D01  play_pause            — "play/pause", "pause the music"
  D02  next_track            — "skip to next track"
  D03  previous_track        — "go back to the previous track"
  D04  play_youtube_music    — "play X on youtube music"
  (catalog's "play a YouTube video" is NOT a separate skill here — C1's
  youtube_search already lists "play" as a trigger verb for "play X on
  youtube," so a distinct skill would be 100% functionally duplicate, doing
  the exact same thing. Found via live regression testing, removed rather
  than kept as redundant code.)
  D06  play_soundcloud       — "play X on soundcloud"
  D07  play_bandcamp         — "play X on bandcamp"
  D08  play_internet_radio   — "play internet radio", "play jazz radio"
  (catalog's "set media volume" is NOT a separate skill either — this
  machine has no per-application audio isolation, so it's the same
  mechanism as A05 set_volume, AND A05's own pattern already matches any
  "volume ... NUMBER" phrasing regardless of the word "media" appearing
  before it, since A is scanned before D. A dedicated matcher here would be
  unreachable dead code, not just a duplicate — found via testing, removed.)
  D10  take_photo            — "take a photo with my webcam"
  D11  open_camera_app       — "open the camera app"
  D12  change_wallpaper      — "change my wallpaper to X" / bare "change my
                                wallpaper" picks randomly from ~/Pictures
  D13  browse_local_media    — "browse my local media" — opens the file
                                manager at ~/Music (a documented
                                interpretation of an inherently vague catalog
                                entry, not a guess pretending to be exact)
"""

import re
from collections.abc import Callable


def _match_play_pause(text: str) -> dict | None:
    if re.search(r"\bplay\s*/\s*pause\b|\bpause\s+(?:the\s+)?(?:music|playback|song)\b"
                 r"|\bresume\s+(?:playback|the\s+music)\b|\btoggle\s+play(?:back)?\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_next_track(text: str) -> dict | None:
    if re.search(r"\bskip\s+to\s+(?:the\s+)?next\s+track\b|\bnext\s+track\b"
                 r"|\bplay\s+the\s+next\s+song\b|\bskip\s+(?:this\s+)?song\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_previous_track(text: str) -> dict | None:
    if re.search(r"\b(?:go\s+back\s+to\s+|play\s+)?(?:the\s+)?previous\s+track\b"
                 r"|\bplay\s+the\s+last\s+song\b|\bprevious\s+song\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_play_youtube_music(text: str) -> dict | None:
    m = re.search(r"\bplay\s+(.+?)\s+on\s+youtube\s+music\b", text, re.IGNORECASE)
    if not m:
        return None
    query = m.group(1).strip().rstrip("?.")
    return {"query": query} if query else None


def _match_play_soundcloud(text: str) -> dict | None:
    m = re.search(r"\bplay\s+(.+?)\s+on\s+soundcloud\b", text, re.IGNORECASE)
    if not m:
        return None
    query = m.group(1).strip().rstrip("?.")
    return {"query": query} if query else None


def _match_play_bandcamp(text: str) -> dict | None:
    m = re.search(r"\bplay\s+(.+?)\s+on\s+bandcamp\b", text, re.IGNORECASE)
    if not m:
        return None
    query = m.group(1).strip().rstrip("?.")
    return {"query": query} if query else None


def _match_play_internet_radio(text: str) -> dict | None:
    m = re.search(
        r"\bplay\s+(?:the\s+)?(?:internet\s+)?radio\b(?:\s*[:,-]?\s*(.+))?"
        r"|\bplay\s+(.+?)\s+radio\s+station\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    return {"query": query or None}


def _match_take_photo(text: str) -> dict | None:
    if re.search(r"\btake\s+a\s+photo(?:\s+with\s+my\s+webcam)?\b"
                 r"|\btake\s+a\s+picture(?:\s+with\s+my\s+webcam)?\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_open_camera_app(text: str) -> dict | None:
    if re.search(r"\bopen\s+(?:the\s+|my\s+)?camera(?:\s+app)?\b", text, re.IGNORECASE):
        return {}
    return None


def _match_change_wallpaper(text: str) -> dict | None:
    m = re.search(
        r"\b(?:change|set)\s+(?:my\s+)?wallpaper\s+to\s+(.+)"
        r"|\b(?:change|set)\s+(?:my\s+)?(?:desktop\s+)?background\s+to\s+(.+)",
        text, re.IGNORECASE
    )
    if m:
        name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
        return {"name": name or None}
    if re.search(r"\b(?:change|randomize)\s+(?:my\s+)?wallpaper\b", text, re.IGNORECASE):
        return {"name": None}
    return None


def _match_browse_local_media(text: str) -> dict | None:
    if re.search(r"\bbrowse\s+(?:my\s+)?local\s+media\b"
                 r"|\bshow\s+me\s+my\s+media\s+files\b",
                 text, re.IGNORECASE):
        return {}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("play_youtube_music", _match_play_youtube_music),
    ("play_soundcloud", _match_play_soundcloud),
    ("play_bandcamp", _match_play_bandcamp),
    ("play_internet_radio", _match_play_internet_radio),
    ("play_pause", _match_play_pause),
    ("next_track", _match_next_track),
    ("previous_track", _match_previous_track),
    ("take_photo", _match_take_photo),
    ("open_camera_app", _match_open_camera_app),
    ("change_wallpaper", _match_change_wallpaper),
    ("browse_local_media", _match_browse_local_media),
]
