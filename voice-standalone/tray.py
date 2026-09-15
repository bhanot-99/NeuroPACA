#!/usr/bin/env python3
"""
Voice Assistant tray icon — sends a toggle signal to daemon.py over a FIFO.

Runs under SYSTEM python3, not voice-standalone/.venv — PyGObject/
AyatanaAppIndicator3 are desktop-shell bindings (apt: gir1.2-
ayatanaappindicator3-0.1), same reasoning as NeuroPaca's own
scripts/neuropaca_tray.py. This process has no other dependency on
voice-standalone's own .venv packages (no google-genai, no fastembed) —
its only job is showing an icon and writing one byte to the FIFO.

PLATFORM NOTE: AppIndicator/StatusNotifierItem is a menu-based protocol on
most desktops — a bare left-click with no menu attached often does nothing
at all (verify this live on the actual desktop rather than assume). This
tray uses a single menu item ("Start Recording" / "Stop Recording," label
reflects current state) as the closest reliable approximation of a toggle
this protocol actually supports.
"""

import os
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, Gtk  # noqa: E402

FIFO_PATH = os.path.expanduser("~/.local/share/voice-standalone/toggle.fifo")

_recording = False
_toggle_item: Gtk.MenuItem


def _send_toggle() -> None:
    """Write in a background thread — a FIFO write blocks until the daemon
    opens it for reading, and the daemon is busy (recording/processing)
    between toggles. Blocking here would freeze the whole tray/GTK UI."""
    def _write() -> None:
        try:
            with open(FIFO_PATH, "w") as f:
                f.write("toggle\n")
        except OSError:
            pass  # daemon not running — nothing sensible to do from the tray
    threading.Thread(target=_write, daemon=True).start()


def _on_toggle(_item: Gtk.MenuItem) -> None:
    global _recording
    _recording = not _recording
    _toggle_item.set_label("Stop Recording" if _recording else "Start Recording")
    _send_toggle()


def _on_quit(_item: Gtk.MenuItem) -> None:
    Gtk.main_quit()


def _build_menu() -> Gtk.Menu:
    global _toggle_item
    menu = Gtk.Menu()

    _toggle_item = Gtk.MenuItem(label="Start Recording")
    _toggle_item.connect("activate", _on_toggle)
    menu.append(_toggle_item)

    menu.append(Gtk.SeparatorMenuItem())

    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", _on_quit)
    menu.append(quit_item)

    menu.show_all()
    return menu


def main() -> None:
    indicator = AyatanaAppIndicator3.Indicator.new(
        "voice-standalone-tray",
        "audio-input-microphone-symbolic",
        AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_menu(_build_menu())
    Gtk.main()


if __name__ == "__main__":
    main()
