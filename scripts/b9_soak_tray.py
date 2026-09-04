#!/usr/bin/env python3
"""B9 soak status, live in the tray -- the same words the login popup shows.

WHY THIS EXISTS

`b9_soak_7day.sh` raises a zenity popup once at session start (and once at
completion). Between those two moments the soak is invisible: the only way to
check on it is to run `b9_soak_7day.sh --status` from a terminal. This puts
the same numbers one glance away in the panel for the rest of the week.

NOT THE L9 TRAY ICON

`Architecture.md` and `phases.md` both name a tray icon as part of the future
L9 Comms module -- a permanent, daemon-driven notification surface wired
through the `EventBus`. This is not that. This is a throwaway B9-hardening
diagnostic for one soak: it reads two files off disk and has no daemon
coupling, no EventBus subscription, no module lifecycle. Delete it once the
soak completes; L9 gets its own tray icon when L9 is built.

ONE SOURCE OF THE NUMBERS

This does not re-derive accrued runtime, memory trend, or session counts. It
imports `b9_soak_state.py` directly and calls the exact `summarise()` function
`b9_soak_7day.sh` calls, with the same `now`, so the tray and the popup can
never say different things about the same soak at the same instant.
`b9_soak_state.py` is pure stdlib (see its own docstring), which is what makes
importing it -- and unit-testing the logic below -- possible without gi.

PURE LOGIC VS. TOOLKIT GLUE

`compute_status()` / `read_status()` / `format_popup_text()` below are plain
functions over stdlib types: no `gi` import, so they run and are tested (see
`tests/test_b9_soak_tray.py`) under the project .venv, which deliberately has
no PyGObject (same reasoning as the lazy `pywayland` import in
`src/neuropaca/sensing/activity/wayland_idle.py`). GTK/AppIndicator only enter
in `_run_tray()`, imported lazily -- that half is verified live on this
machine, not by pytest, same split as the real Wayland path.

WHY SYSTEM PYTHON, NOT THE PROJECT .venv

PyGObject + AyatanaAppIndicator3 are desktop-shell bindings (apt packages:
gir1.2-ayatanaappindicator3-0.1), not a neuropaca runtime dependency, and the
project .venv is built with --system-site-packages off. Run this with the
system `python3`, the same one COSMIC's own panel applets use:

    scripts/b9_soak_tray.py &

or enable scripts/systemd/neuropaca-b9-soak-tray.service to have it start
with the session, the same way neuropaca-b9-soak.service does.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SOAK_DIR = REPO / "data" / "b9_soak"
STATE_PATH = SOAK_DIR / "state.json"
SAMPLES_PATH = SOAK_DIR / "samples.jsonl"
REFRESH_SECONDS = 60

ICON_RUNNING = "utilities-system-monitor"
ICON_COMPLETE = "emblem-default"
ICON_DAEMON_DOWN = "dialog-warning"
ICON_ERROR = "dialog-error"


def _load_soak_state_module() -> Any:
    """Import scripts/b9_soak_state.py by path.

    It has no `__init__.py` sibling -- it isn't a package -- and this file is
    meant to run standalone under system python3 without the project on
    PYTHONPATH, so a plain `import b9_soak_state` isn't available either.
    """
    module_path = REPO / "scripts" / "b9_soak_state.py"
    spec = importlib.util.spec_from_file_location("b9_soak_state", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: b9_soak_state.py defines a slotted dataclass,
    # and dataclasses resolves annotations through sys.modules[cls.__module__]
    # -- skip this and it dies with "'NoneType' object has no attribute
    # '__dict__'". Same fix tests/test_b9_soak_state.py already applies.
    sys.modules["b9_soak_state"] = module
    spec.loader.exec_module(module)
    return module


soak_state = _load_soak_state_module()


@dataclass(frozen=True)
class SoakStatus:
    """Everything one tray refresh needs to render. Plain data -- nothing
    here touches a GTK object, which is what makes it constructible in a
    test without gi installed."""

    text: str  # exact `summarise()` output -- what the popup shows, verbatim
    pct: float  # 0..100, clamped
    completed: bool
    daemon_up: bool
    icon_name: str
    label: str  # panel text, e.g. " 5.0%"


def compute_status(
    state: dict[str, Any],
    samples: list[dict[str, Any]],
    now: datetime | None = None,
) -> SoakStatus:
    """Derive icon/label/menu content from soak state.

    Takes the same `now` `summarise()` uses (defaulting the same way, at the
    same call) so the popup text and the tray's own pct/icon can never read
    two different instants of "now" -- the discrepancy would only ever be
    microseconds, but there is no reason to allow even that.
    """
    text = soak_state.summarise(state, samples, now)
    completed = bool(state.get("completed_utc"))
    accrued = soak_state.accrued_seconds(state, now)
    pct = min(100.0, 100.0 * accrued / state["target_seconds"])
    daemon_up = bool(samples[-1].get("daemon_up", True)) if samples else True

    if completed:
        icon = ICON_COMPLETE
    elif not daemon_up:
        icon = ICON_DAEMON_DOWN
    else:
        icon = ICON_RUNNING

    return SoakStatus(
        text=text,
        pct=pct,
        completed=completed,
        daemon_up=daemon_up,
        icon_name=icon,
        label=f"{pct:4.1f}%",
    )


def read_status(state_path: Path = STATE_PATH, samples_path: Path = SAMPLES_PATH) -> SoakStatus:
    """Read the on-disk soak state and turn it into a `SoakStatus`.

    Never raises. A soak that has produced nothing readable for a week is
    itself the finding B7 taught this project to never look away from --
    the tray shows that as a visible warning status, not a silent crash that
    just makes the icon disappear.
    """
    try:
        state = soak_state.load_state(state_path)
        samples = soak_state.read_samples(samples_path) if samples_path.exists() else []
        return compute_status(state, samples)
    except Exception as exc:
        return SoakStatus(
            text=f"soak state unreadable:\n{exc}",
            pct=0.0,
            completed=False,
            daemon_up=False,
            icon_name=ICON_ERROR,
            label=" ERR",
        )


def format_popup_text(text: str) -> str:
    """zenity `--text` is Pango markup; escape the summary before wrapping it
    in `<tt>` -- the same escaping `b9_soak_7day.sh`'s `show_popup()` does."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<tt>{escaped}</tt>"


def raise_popup(text: str) -> bool:
    """Show the same dialog `b9_soak_7day.sh` raises at login.

    Returns False and does nothing if zenity isn't installed -- mirrors the
    shell driver's own "zenity absent -- popup skipped" behaviour rather than
    failing loudly for something that was never available anyway.
    """
    if not shutil.which("zenity"):
        return False
    subprocess.Popen(  # fixed argv, no shell, no user-controlled input
        [
            "zenity",
            "--info",
            "--title=NeuroPACA · B9 7-day soak",
            "--width=560",
            "--ok-label=OK",
            f"--text={format_popup_text(text)}",
        ],
        start_new_session=True,
    )
    return True


# ------------------------------------------------------------------------------
# GTK glue -- imports gi lazily so everything above stays importable (and
# tested) under the project .venv, which has no PyGObject by design. Verified
# live on this machine; not exercised by pytest, same split rules.md accepts
# for the real Wayland path in sensing/activity/.
# ------------------------------------------------------------------------------


def _run_tray() -> None:
    import signal

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    from gi.repository import GLib, Gtk

    class SoakTray:
        def __init__(self) -> None:
            self.indicator = AppIndicator3.Indicator.new(
                "neuropaca-b9-soak",
                ICON_RUNNING,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.zenity_available = shutil.which("zenity") is not None
            self.refresh()
            GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)

        def _on_timer(self) -> bool:
            self.refresh()
            return True  # GLib.SOURCE_CONTINUE -- keep the timer running

        def refresh(self) -> None:
            status = read_status()
            self.indicator.set_icon_full(status.icon_name, "B9 soak status")
            self.indicator.set_label(status.label, "100.0%")
            self.indicator.set_menu(self._build_menu(status.text))

        def _build_menu(self, text: str) -> Gtk.Menu:
            menu = Gtk.Menu()

            header = Gtk.MenuItem(label="NeuroPACA · B9 7-day soak")
            header.set_sensitive(False)
            menu.append(header)
            menu.append(Gtk.SeparatorMenuItem())

            # Same lines the popup shows, one per menu row -- nothing is
            # reworded or summarised-of-a-summary on the way into the tray.
            for line in text.splitlines():
                item = Gtk.MenuItem(label=line if line else " ")
                item.set_sensitive(False)
                menu.append(item)

            menu.append(Gtk.SeparatorMenuItem())

            if self.zenity_available:
                popup_item = Gtk.MenuItem(label="Show full popup")
                popup_item.connect("activate", lambda *_: raise_popup(text))
                menu.append(popup_item)
            else:
                dead = Gtk.MenuItem(label="(zenity not installed -- no popup)")
                dead.set_sensitive(False)
                menu.append(dead)

            refresh_item = Gtk.MenuItem(label="Refresh now")
            refresh_item.connect("activate", lambda *_: self.refresh())
            menu.append(refresh_item)

            menu.append(Gtk.SeparatorMenuItem())

            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", lambda *_: Gtk.main_quit())
            menu.append(quit_item)

            menu.show_all()
            return menu

    # No variable holds the instance -- `GLib.timeout_add_seconds` above
    # already keeps it alive via the bound `self._on_timer` reference, for as
    # long as the timer itself is alive (i.e. for the life of the process).
    SoakTray()

    # Exit promptly on SIGTERM/SIGINT (systemctl --user stop, Ctrl-C) instead
    # of the process being killed mid-loop. The tray holds no state to flush,
    # but a clean Gtk.main_quit() exits well inside TimeoutStopSec rather than
    # needing it, and matches the discipline neuropaca-b9-soak.service already
    # applies to its own SIGTERM handling.
    def _quit(*_args: object) -> bool:
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)

    Gtk.main()


def main() -> int:
    _run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
