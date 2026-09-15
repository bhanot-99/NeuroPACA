"""
Category A — System & power control (24 skills).

Skills implemented:
  A01  volume_up          — "volume up", "turn up the volume"
  A02  volume_down        — "volume down", "turn down the volume"
  A03  mute               — "mute", "mute the volume", "mute audio"
  A04  unmute             — "unmute", "unmute the volume"
  A05  set_volume         — "set volume to 60%", "volume 70 percent"
  A06  brightness_up      — "brightness up", "increase brightness"
  A07  brightness_down    — "brightness down", "decrease brightness"
  A08  set_brightness     — "set brightness to 80%", "brightness 50"
  A09  toggle_dark_mode   — "dark mode", "switch to dark mode", "light mode"
  A10  lock_screen        — "lock screen", "lock the screen"
  A11  shutdown           — "shut down", "power off"  🔒
  A12  restart            — "restart", "reboot"       🔒
  A13  logout             — "log out", "logout"
  A14  sleep              — "sleep", "suspend"
  A15  screenshot         — "take a screenshot", "screenshot"
  A16  start_recording    — "start screen recording"
  A17  stop_recording     — "stop screen recording"
  A18  toggle_wifi        — "wifi on/off", "toggle wifi"
  A19  toggle_bluetooth   — "bluetooth on/off", "toggle bluetooth"
  A20  toggle_dnd         — "do not disturb on/off", "toggle do not disturb"
  A21  toggle_airplane    — "airplane mode on/off", "toggle airplane mode"
  A22  toggle_night_light — "night light on/off", "toggle night light"  ★see note
  A23  battery_status     — "battery status", "how much battery"
  A24  toggle_kbd_backlight— "keyboard backlight on/off", "toggle keyboard light"
  A25  toggle_mic_mute    — "mute mic", "unmute microphone", "mic mute toggle"
  A26  toggle_display     — "toggle external display", "mirror display"

NOTE A22 (night_light): COSMIC does not currently expose night-light / colour
temperature adjustment through any accessible D-Bus interface or CLI tool.
The gsettings key org.gnome.settings-daemon.plugins.color is present in the
schema list but the GSD colour plugin does not run under COSMIC's compositor.
The matcher is implemented (returns kwargs on a confident hit) but the
executor in actions.py explicitly documents the limitation and is a no-op.
"""

import re
from collections.abc import Callable

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _kw(*words: str) -> str:
    """Build a regex alternation of whole-word keywords."""
    return r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"


# ---------------------------------------------------------------------------
# A01 — volume_up
# ---------------------------------------------------------------------------

def _match_volume_up(text: str) -> dict | None:
    if re.search(
        r"\b(?:turn\s+up|increase|raise|boost)\s+(?:the\s+)?volume"
        r"|\bvolume\s+up\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A02 — volume_down
# ---------------------------------------------------------------------------

def _match_volume_down(text: str) -> dict | None:
    if re.search(
        r"\b(?:turn\s+down|decrease|lower|reduce)\s+(?:the\s+)?volume"
        r"|\bvolume\s+down\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A03 — mute
# ---------------------------------------------------------------------------

def _match_mute(text: str) -> dict | None:
    # Must NOT match "unmute"
    if re.search(r"(?<!\bun)\bmute\b(?!\s+mic|\s+microphone)", text, re.IGNORECASE):
        # Exclude "mute mic" — that's A25
        if re.search(r"\bmute\b", text, re.IGNORECASE) and not re.search(
            r"\bunmute\b|\bmic\b|\bmicrophone\b", text, re.IGNORECASE
        ):
            return {}
    return None


# ---------------------------------------------------------------------------
# A04 — unmute
# ---------------------------------------------------------------------------

def _match_unmute(text: str) -> dict | None:
    if re.search(r"\bunmute\b(?!\s+mic|\s+microphone)", text, re.IGNORECASE):
        if not re.search(r"\bmic\b|\bmicrophone\b", text, re.IGNORECASE):
            return {}
    return None


# ---------------------------------------------------------------------------
# A05 — set_volume  (specific percentage → goes before up/down in the list)
# ---------------------------------------------------------------------------

def _match_set_volume(text: str) -> dict | None:
    m = re.search(
        r"\b(?:set\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})\s*(?:%|percent)?\b"
        r"|\bvolume\s+(?:to\s+)?(\d{1,3})\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    percent = int(raw)
    if not (0 <= percent <= 100):
        return None
    return {"percent": percent}


# ---------------------------------------------------------------------------
# A06 — brightness_up
# ---------------------------------------------------------------------------

def _match_brightness_up(text: str) -> dict | None:
    if re.search(
        r"\b(?:turn\s+up|increase|raise|boost)\s+(?:the\s+)?brightness"
        r"|\bbrightness\s+up\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A07 — brightness_down
# ---------------------------------------------------------------------------

def _match_brightness_down(text: str) -> dict | None:
    if re.search(
        r"\b(?:turn\s+down|decrease|lower|reduce|dim)\s+(?:the\s+)?brightness"
        r"|\bbrightness\s+down\b"
        r"|\bdim\s+(?:the\s+)?(?:screen|display)\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A08 — set_brightness  (specific percentage)
# ---------------------------------------------------------------------------

def _match_set_brightness(text: str) -> dict | None:
    m = re.search(
        r"\b(?:set\s+)?brightness\s+(?:to\s+|at\s+)?(\d{1,3})\s*(?:%|percent)?\b"
        r"|\bbrightness\s+(?:to\s+)?(\d{1,3})\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    percent = int(raw)
    if not (0 <= percent <= 100):
        return None
    return {"percent": percent}


# ---------------------------------------------------------------------------
# A09 — toggle_dark_mode
# ---------------------------------------------------------------------------

def _match_toggle_dark_mode(text: str) -> dict | None:
    m = re.search(
        r"\b(?:switch\s+to\s+|enable\s+|turn\s+on\s+|activate\s+)?"
        r"(dark|light)\s+(?:mode|theme)\b"
        r"|\btoggle\s+(?:dark|light)\s+(?:mode|theme)\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    # Determine target mode from text
    is_dark = "light" not in text.lower()
    return {"dark": is_dark}


# ---------------------------------------------------------------------------
# A10 — lock_screen
# ---------------------------------------------------------------------------

def _match_lock_screen(text: str) -> dict | None:
    if re.search(
        r"\block\s+(?:the\s+|my\s+)?(?:screen|computer|pc|laptop|system)\b"
        r"|\bscreen\s+lock\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A11 — shutdown  🔒
# ---------------------------------------------------------------------------

def _match_shutdown(text: str) -> dict | None:
    if re.search(r"\b(?:shut\s*down|power\s+off|turn\s+off\s+(?:the\s+)?(?:computer|pc|laptop|system))\b",
                 text, re.IGNORECASE):
        return {}
    return None


# ---------------------------------------------------------------------------
# A12 — restart  🔒
# ---------------------------------------------------------------------------

def _match_restart(text: str) -> dict | None:
    # Bare "reboot" or "restart [the] [device]" — but NOT "restart a service/daemon/process"
    if not re.search(r"\bservice\b|\bdaemon\b|\bprocess\b", text, re.IGNORECASE):
        if re.search(
            r"\breboot\b"
            r"|\brestart\b(?:\s+(?:my\s+|the\s+)?(?:computer|pc|laptop|system))?",
            text, re.IGNORECASE
        ):
            return {}
    return None


# ---------------------------------------------------------------------------
# A13 — logout
# ---------------------------------------------------------------------------

def _match_logout(text: str) -> dict | None:
    if re.search(r"\blog\s*out\b|\bsign\s*out\b|\blogoff\b", text, re.IGNORECASE):
        return {}
    return None


# ---------------------------------------------------------------------------
# A14 — sleep
# ---------------------------------------------------------------------------

def _match_sleep(text: str) -> dict | None:
    if re.search(
        r"\bsleep\b"
        r"|\bsuspend\b"
        r"|\bput\s+(?:the\s+|my\s+)?(?:computer|pc|laptop|system)\s+to\s+sleep\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A15 — screenshot
# ---------------------------------------------------------------------------

def _match_screenshot(text: str) -> dict | None:
    if re.search(r"\b(?:take\s+(?:a\s+)?)?screenshot\b"
                 r"|\bcapture\s+(?:the\s+)?screen\b"
                 r"|\bscreen\s+(?:capture|grab|shot)\b",
                 text, re.IGNORECASE):
        return {}
    return None


# ---------------------------------------------------------------------------
# A16 — start_recording
# ---------------------------------------------------------------------------

def _match_start_recording(text: str) -> dict | None:
    if re.search(r"\bstart\s+(?:screen\s+)?recording\b"
                 r"|\brecord\s+(?:the\s+)?(?:screen|display)\b"
                 r"|\bbegin\s+(?:screen\s+)?recording\b",
                 text, re.IGNORECASE):
        return {}
    return None


# ---------------------------------------------------------------------------
# A17 — stop_recording
# ---------------------------------------------------------------------------

def _match_stop_recording(text: str) -> dict | None:
    if re.search(r"\bstop\s+(?:screen\s+)?recording\b"
                 r"|\bend\s+(?:screen\s+)?recording\b"
                 r"|\bfinish\s+recording\b",
                 text, re.IGNORECASE):
        return {}
    return None


# ---------------------------------------------------------------------------
# A18 — toggle_wifi
# ---------------------------------------------------------------------------

_QUESTION_WORDS = r"\b(?:is|are|was|were)\b"


def _match_toggle_wifi(text: str) -> dict | None:
    # The verb must sit directly next to "wifi" — a bare "switch"/"turn"
    # anywhere in the sentence (e.g. "switch to firefox") must NOT count.
    m = re.search(
        r"\bturn\s+(on|off)\s+(?:the\s+)?wi[-\s]?fi\b"
        r"|\bwi[-\s]?fi\s+(on|off)\b"
        r"|\b(enable|disable)\s+(?:the\s+)?wi[-\s]?fi\b"
        r"|\btoggle\s+(?:the\s+)?wi[-\s]?fi\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    # "is wifi on?" is a status question, not a command — don't act on it
    # just because "wifi on" happens to appear adjacent.
    if re.search(_QUESTION_WORDS + r"[^.?!]{0,20}\bwi[-\s]?fi\b[^.?!]{0,10}\b(?:on|off)\b",
                 text, re.IGNORECASE) and not re.search(
                 r"\bturn\b|\benable\b|\bdisable\b|\btoggle\b", text, re.IGNORECASE):
        return None
    word = (m.group(1) or m.group(2) or m.group(3) or "").lower()
    state = "on" if word in ("on", "enable") else ("off" if word in ("off", "disable") else None)
    return {"state": state}


# ---------------------------------------------------------------------------
# A19 — toggle_bluetooth
# ---------------------------------------------------------------------------

def _match_toggle_bluetooth(text: str) -> dict | None:
    # Same anchoring fix as toggle_wifi — verb must sit next to "bluetooth".
    m = re.search(
        r"\bturn\s+(on|off)\s+(?:the\s+)?bluetooth\b"
        r"|\bbluetooth\s+(on|off)\b"
        r"|\b(enable|disable)\s+(?:the\s+)?bluetooth\b"
        r"|\btoggle\s+(?:the\s+)?bluetooth\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    # Same "is bluetooth on?" status-question exclusion as toggle_wifi.
    if re.search(_QUESTION_WORDS + r"[^.?!]{0,20}\bbluetooth\b[^.?!]{0,10}\b(?:on|off)\b",
                 text, re.IGNORECASE) and not re.search(
                 r"\bturn\b|\benable\b|\bdisable\b|\btoggle\b", text, re.IGNORECASE):
        return None
    word = (m.group(1) or m.group(2) or m.group(3) or "").lower()
    state = "on" if word in ("on", "enable") else ("off" if word in ("off", "disable") else None)
    return {"state": state}


# ---------------------------------------------------------------------------
# A20 — toggle_dnd  (Do Not Disturb)
# ---------------------------------------------------------------------------

def _match_toggle_dnd(text: str) -> dict | None:
    if re.search(
        r"\bdo\s+not\s+disturb\b"
        r"|\bDND\b"
        r"|\bnotifications?\s+(?:off|on|toggle)\b"
        r"|\b(?:disable|enable)\s+notifications?\b",
        text, re.IGNORECASE
    ):
        on = re.search(r"\bon\b|\benable\b", text, re.IGNORECASE) is not None
        off = re.search(r"\boff\b|\bdisable\b", text, re.IGNORECASE) is not None
        state: str | None = "on" if on and not off else ("off" if off and not on else None)
        return {"state": state}
    return None


# ---------------------------------------------------------------------------
# A21 — toggle_airplane
# ---------------------------------------------------------------------------

def _match_toggle_airplane(text: str) -> dict | None:
    if re.search(r"\bairplane\s+mode\b"
                 r"|\bflight\s+mode\b"
                 r"|\btoggle\s+(?:all\s+)?radios\b",
                 text, re.IGNORECASE):
        on = re.search(r"\bon\b|\benable\b", text, re.IGNORECASE) is not None
        off = re.search(r"\boff\b|\bdisable\b", text, re.IGNORECASE) is not None
        state: str | None = "on" if on and not off else ("off" if off and not on else None)
        return {"state": state}
    return None


# ---------------------------------------------------------------------------
# A22 — toggle_night_light
# NOTE: COSMIC does not expose night-light control via D-Bus or any available
# CLI.  The matcher fires correctly; the executor in actions.py is a documented
# no-op until COSMIC gains a stable interface.
# ---------------------------------------------------------------------------

def _match_toggle_night_light(text: str) -> dict | None:
    if re.search(
        r"\bnight\s+(?:light|mode|shift)\b"
        r"|\bwarm\s+(?:light|display|screen)\b"
        r"|\bblue\s+light\s+filter\b"
        r"|\btoggle\s+night\b",
        text, re.IGNORECASE
    ):
        on = re.search(r"\bon\b|\benable\b", text, re.IGNORECASE) is not None
        off = re.search(r"\boff\b|\bdisable\b", text, re.IGNORECASE) is not None
        state: str | None = "on" if on and not off else ("off" if off and not on else None)
        return {"state": state}
    return None


# ---------------------------------------------------------------------------
# A23 — battery_status
# ---------------------------------------------------------------------------

def _match_battery_status(text: str) -> dict | None:
    # "health" deliberately removed from this trigger list — Step 4 added a
    # distinct battery_health skill (L01, capacity/wear via upower, a real,
    # different thing from charge %); found via testing that this pattern's
    # original "health" trigger (written in Step 1, before L01 existed)
    # would otherwise always win since category A is scanned before L.
    if re.search(
        r"\bbattery\s+(?:status|level|percentage|life|charge|remaining|info)\b"
        r"|\bhow\s+much\s+battery\b"
        r"|\bis\s+(?:my\s+)?(?:laptop\s+)?(?:battery\s+)?charging\b"
        r"|\bcheck\s+(?:my\s+)?battery\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# A24 — toggle_kbd_backlight
# ---------------------------------------------------------------------------

def _match_toggle_kbd_backlight(text: str) -> dict | None:
    if re.search(
        r"\bkeyboard\s+(?:backlight|light|lighting|illumination)\b"
        r"|\bkbd\s+(?:backlight|light)\b"
        r"|\btoggle\s+keyboard\s+(?:backlight|light)\b",
        text, re.IGNORECASE
    ):
        on = re.search(r"\bon\b|\benable\b|\bincrease\b|\bup\b", text, re.IGNORECASE) is not None
        off = re.search(r"\boff\b|\bdisable\b|\bdecrease\b|\bdown\b", text, re.IGNORECASE) is not None
        state: str | None = "on" if on and not off else ("off" if off and not on else None)
        return {"state": state}
    return None


# ---------------------------------------------------------------------------
# A25 — toggle_mic_mute
# ---------------------------------------------------------------------------

def _match_toggle_mic_mute(text: str) -> dict | None:
    m = re.search(
        r"\b(?:mute|unmute|toggle)\s+(?:the\s+)?(?:mic(?:rophone)?)\b"
        r"|\bmic(?:rophone)?\s+(?:mute|unmute|toggle|on|off)\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    mute = re.search(r"\bmute\b(?!\s*\bun)", text, re.IGNORECASE) is not None
    unmute = re.search(r"\bunmute\b", text, re.IGNORECASE) is not None
    state: str | None = "mute" if mute and not unmute else ("unmute" if unmute and not mute else None)
    return {"state": state}


# ---------------------------------------------------------------------------
# A26 — toggle_display  (external display / mirroring)
# ---------------------------------------------------------------------------

def _match_toggle_display(text: str) -> dict | None:
    if re.search(
        r"\b(?:external|second(?:ary)?|hdmi|dp|displayport)\s+(?:display|monitor|screen)\b"
        r"|\b(?:toggle|mirror|extend|duplicate)\s+(?:the\s+)?(?:display|screen|monitor)\b"
        r"|\bdisplay\s+(?:mirror|extend|toggle)\b",
        text, re.IGNORECASE
    ):
        mode: str | None = None
        if re.search(r"\bmirror\b|\bduplicate\b", text, re.IGNORECASE):
            mode = "mirror"
        elif re.search(r"\bextend\b", text, re.IGNORECASE):
            mode = "extend"
        return {"mode": mode}
    return None


# ---------------------------------------------------------------------------
# Ordered skill registry for this category.
# More-specific patterns first to avoid false matches on looser ones.
# ---------------------------------------------------------------------------

SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    # Volume — set_volume before up/down/mute/unmute (has percentage anchor)
    ("set_volume", _match_set_volume),
    ("volume_up", _match_volume_up),
    ("volume_down", _match_volume_down),
    # Mute — unmute before mute (unmute is more specific prefix)
    ("unmute", _match_unmute),
    ("mute", _match_mute),
    # Brightness — set_brightness before up/down (percentage anchor)
    ("set_brightness", _match_set_brightness),
    ("brightness_up", _match_brightness_up),
    ("brightness_down", _match_brightness_down),
    # System state
    ("toggle_dark_mode", _match_toggle_dark_mode),
    ("lock_screen", _match_lock_screen),
    ("shutdown", _match_shutdown),
    ("restart", _match_restart),
    ("logout", _match_logout),
    ("sleep", _match_sleep),
    # Screen
    ("screenshot", _match_screenshot),
    ("start_recording", _match_start_recording),
    ("stop_recording", _match_stop_recording),
    ("toggle_display", _match_toggle_display),
    # Connectivity
    ("toggle_wifi", _match_toggle_wifi),
    ("toggle_bluetooth", _match_toggle_bluetooth),
    ("toggle_airplane", _match_toggle_airplane),
    # Notifications / focus
    ("toggle_dnd", _match_toggle_dnd),
    ("toggle_night_light", _match_toggle_night_light),
    # Hardware
    ("battery_status", _match_battery_status),
    ("toggle_kbd_backlight", _match_toggle_kbd_backlight),
    ("toggle_mic_mute", _match_toggle_mic_mute),
]
