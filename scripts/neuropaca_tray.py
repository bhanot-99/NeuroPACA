#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the presence tray — a permanent, passive status icon
(VISION_PHASES.md, "Living presence").

WHAT THIS IS

A glance at the corner of your screen tells you what NeuroPACA is doing right
now — thinking, noticed something, focused on your work, idle, or just
awake. One click opens the graph view; another forces an immediate refresh.

REBUILT, PASSIVE, NO SOCKET

The original version of this tray talked to `interface/layer.py` over a Unix
socket — it could pause notifications and give thumbs-up/down feedback on
the last thing the daemon said, in addition to showing status. That whole
socket/CLI surface was removed by user decision (no terminal/text control
going forward — voice is the planned replacement), and with it went every
op this tray used to *write* to (`pause`, `feedback`, the on-demand
`mirror`). Rebuilding those needs a write-back channel this tray does not
have any more.

What's left, and what this rebuild keeps: the *read* side.
`orchestration/orchestrator.py` now periodically writes its own
`health_check()` as JSON to `config.health_dump_path` (atomically), and one
of the modules in that report is `presence` (`core/presence_tracker.py`) — a
small always-on subscriber that computes exactly the same state machine
(`core/presence.py`) the old L9 code did, from the same events, with nowhere
to write back to. This tray reads that file. No pause, no feedback, no
on-demand mirror — a status display, not a control surface.

There is a *second*, unrelated tray on this machine
(`scripts/soak_tray.py`) — the temporary B9 soak-hardening widget (graph
button, basic daemon info, a refresh button), reading the same kind of
health-dump data plus the soak's own progress files. The two are
independent, deliberately: `soak_tray.py` is meant to be deleted once its
soak completes, and this one is permanent.

PURE LOGIC VS. TOOLKIT GLUE

`read_health()`, `compute_tray_view()`, `menu_header()` below are plain
functions over stdlib types — no `gi` import, so they run and are
unit-tested (`tests/test_neuropaca_tray.py`) under the project .venv, which
deliberately has no PyGObject. GTK/AppIndicator only enter in `_run_tray()`,
imported lazily — that half is verified live on this machine, not by
pytest. Same split `soak_tray.py` established.

WHY SYSTEM PYTHON, NOT THE PROJECT .venv

PyGObject + AyatanaAppIndicator3 are desktop-shell bindings (apt package:
gir1.2-ayatanaappindicator3-0.1), not a neuropaca runtime dependency, and the
project .venv is built with --system-site-packages off:

    scripts/neuropaca_tray.py &

or enable scripts/systemd/neuropaca-tray.service.

POLL, NOT PUSH

Every `POLL_SECONDS` (5s) this re-reads the health-dump file. A local file
read at that cadence is not measurable CPU — cheaper than the original
socket round trip, not more expensive.

SURVIVING A RESTART, AND A STALE FILE

`read_health()` returns `None` on any read failure — file absent, malformed,
permission denied. A file that exists but has not been refreshed in a while
(the daemon died without cleaning up) is treated the same way: `is_stale()`
compares its mtime against `now`. Both render as "asleep" in
`compute_tray_view()`, never a crash and never a stale "focused" icon frozen
from before the daemon went away.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
GRAPH_RENDER = REPO / "scripts" / "neuropaca_graph.py"
GRAPH_HTML = REPO / "data" / "graph_view.html"

POLL_SECONDS = 5
# A few missed ticks of the daemon's default `health_dump_interval_seconds`
# (30s) — long enough that one slow write is not mistaken for the daemon
# being gone, short enough that a genuinely dead daemon is caught quickly.
STALE_AFTER_SECONDS = 90.0

ICON_THINKING = "view-refresh"
ICON_NOTICED = "mail-unread"
ICON_FOCUSED = "user-available"
ICON_IDLE = "user-idle"
ICON_AWAKE = "user-available"
ICON_ASLEEP = "user-offline"  # the daemon is unreachable/stale — not one of the five real states
ICON_ERROR = "dialog-error"

_ICON_BY_STATE = {
    "thinking": ICON_THINKING,
    "noticed": ICON_NOTICED,
    "focused": ICON_FOCUSED,
    "idle": ICON_IDLE,
    "awake": ICON_AWAKE,
}

# The widest label this tray ever actually shows ("Thinking", 8 chars) — the
# fixed sizing hint AppIndicator3.set_label's second argument wants, so the
# panel does not resize on every state change.
LABEL_WIDTH_GUIDE = "Thinking"

# Same fallback chain as soak_tray.py, duplicated rather than imported —
# see the module docstring for why the two scripts stay independent.
BRAVE_BINARIES = ("brave-browser", "brave", "brave-browser-stable")


def default_health_dump_path() -> Path:
    override = os.environ.get("NEUROPACA_HEALTH_DUMP")
    if override:
        return Path(override)
    return REPO / "data" / "health.json"


def read_health(path: Path) -> dict[str, Any] | None:
    """The health-dump file, parsed — `None` on *any* failure (absent,
    unreadable, malformed), so the caller treats every failure mode
    uniformly as "no data", never a crash."""
    try:
        parsed = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def is_stale(mtime: float, now: float, *, max_age_seconds: float = STALE_AFTER_SECONDS) -> bool:
    return (now - mtime) > max_age_seconds


def _find_module(health: dict[str, Any], name: str) -> dict[str, Any] | None:
    for module in health.get("modules", []):
        if isinstance(module, dict) and module.get("name") == name:
            return module
    return None


def _parse_kv(detail: str) -> dict[str, str]:
    """`"state=thinking · 0 errors"` -> `{"state": "thinking"}` — only the
    `key=value` tokens are picked up; `·`, `0`, and `errors` have no `=` and
    are silently skipped. Permissive by construction, same reasoning as
    `soak_probe.py`'s own `parse_counters`: a token it does not recognise is
    simply dropped, never a raised exception over a cosmetic rewording of a
    health detail string."""
    parsed: dict[str, str] = {}
    for token in detail.split():
        if "=" in token:
            key, _, value = token.partition("=")
            parsed[key] = value
    return parsed


@dataclass(frozen=True, slots=True)
class TrayView:
    """Everything one refresh needs to render. Plain data — nothing here
    touches a GTK object, which is what makes it constructible in a test
    without gi."""

    state: str  # one of the five presence states, or "asleep" / "unknown"
    icon_name: str
    label: str
    since: str  # ISO timestamp string, or "" if unknown
    error: str | None


def compute_tray_view(health: dict[str, Any] | None, *, stale: bool) -> TrayView:
    """Pure: no file I/O, no gi. `health` is exactly `read_health()`'s
    return; `stale` is `is_stale()`'s verdict on the file's mtime."""
    if health is None or stale:
        return TrayView(
            state="asleep",
            icon_name=ICON_ASLEEP,
            label="Asleep",
            since="",
            error="daemon unreachable" if health is None else "health dump is stale",
        )
    if not health.get("ok"):
        return TrayView(
            state="error",
            icon_name=ICON_ERROR,
            label="Error",
            since="",
            error="daemon reports unhealthy",
        )

    presence = _find_module(health, "presence")
    if presence is None:
        return TrayView(
            state="unknown",
            icon_name=ICON_AWAKE,
            label="Awake",
            since="",
            error=None,
        )
    parsed = _parse_kv(str(presence.get("detail", "")))
    state = parsed.get("state", "awake")
    # `since` is a real, structured field (`last_event_at`), not part of
    # `detail` — a timestamp ending in digits right before the next
    # `key=value` token would otherwise look exactly like
    # `soak_probe.py`'s generic counter regex wants (see
    # `core/presence_tracker.py`'s `health()` docstring for the bug this
    # avoids).
    return TrayView(
        state=state,
        icon_name=_ICON_BY_STATE.get(state, ICON_AWAKE),
        label=state.capitalize(),
        since=str(presence.get("last_event_at") or ""),
        error=None,
    )


def menu_header(view: TrayView) -> str:
    if view.error:
        return f"NeuroPACA · {view.label} ({view.error})"
    return f"NeuroPACA · {view.label}"


def _brave_command() -> list[str] | None:
    for name in BRAVE_BINARIES:
        found = shutil.which(name)
        if found:
            return [found]
    if shutil.which("flatpak") and _flatpak_has("com.brave.Browser"):
        return ["flatpak", "run", "com.brave.Browser"]
    return None


def _flatpak_has(app_id: str) -> bool:
    try:
        done = subprocess.run(
            ["flatpak", "info", app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_graph_view() -> bool:
    """Regenerate the self-contained HTML graph and open it in Brave — the
    same two-step `soak_tray.py` uses, duplicated rather than imported (see
    the module docstring)."""
    if not GRAPH_RENDER.exists():
        return False
    try:
        done = subprocess.run(
            [sys.executable, str(GRAPH_RENDER), "--no-open", "--out", str(GRAPH_HTML)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0 or not GRAPH_HTML.exists():
        return False

    opener = _brave_command()
    if opener is None:
        if not shutil.which("xdg-open"):
            return False
        opener = ["xdg-open"]
    subprocess.Popen(  # fixed argv, no shell
        [*opener, GRAPH_HTML.resolve().as_uri()],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


# ------------------------------------------------------------------------------
# GTK glue -- imports gi lazily so everything above stays importable (and
# tested) under the project .venv, which has no PyGObject by design. Verified
# live on this machine; not exercised by pytest, same split soak_tray.py uses.
# ------------------------------------------------------------------------------


def _run_tray() -> None:
    import signal

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    from gi.repository import GLib, Gtk

    dump_path = default_health_dump_path()

    class PresenceTray:
        def __init__(self) -> None:
            self.indicator = AppIndicator3.Indicator.new(
                "neuropaca",
                ICON_ASLEEP,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.view = compute_tray_view(None, stale=False)
            self.menu = Gtk.Menu()
            self.indicator.set_menu(self.menu)
            self.refresh()
            GLib.timeout_add_seconds(POLL_SECONDS, self._on_timer)

        def _on_timer(self) -> bool:
            self.refresh()
            return True  # GLib.SOURCE_CONTINUE

        def refresh(self) -> None:
            health = read_health(dump_path)
            stale = False
            try:
                stale = is_stale(dump_path.stat().st_mtime, time.time())
            except OSError:
                stale = True
            self.view = compute_tray_view(health, stale=stale)
            self.indicator.set_icon_full(self.view.icon_name, self.view.label)
            self.indicator.set_label(self.view.label, LABEL_WIDTH_GUIDE)
            self._populate_menu()

        def _on_refresh_clicked(self, *_args: object) -> None:
            # Hand the rebuild to the next idle turn so the menu is not torn
            # down from inside its own item's `activate` emission (same
            # reasoning as soak_tray.py's own `_on_refresh_clicked`).
            GLib.idle_add(self._refresh_once)

        def _refresh_once(self) -> bool:
            self.refresh()
            return GLib.SOURCE_REMOVE

        def _populate_menu(self) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)
            view = self.view

            header = Gtk.MenuItem(label=menu_header(view))
            header.set_sensitive(False)
            self.menu.append(header)

            if view.since:
                since_item = Gtk.MenuItem(label=f"since {view.since}")
                since_item.set_sensitive(False)
                self.menu.append(since_item)

            self.menu.append(Gtk.SeparatorMenuItem())

            graph_item = Gtk.MenuItem(label="Open graph view")
            graph_item.connect("activate", lambda *_: open_graph_view())
            self.menu.append(graph_item)

            self.menu.append(Gtk.SeparatorMenuItem())
            refresh_item = Gtk.MenuItem(label="Refresh now")
            refresh_item.connect("activate", self._on_refresh_clicked)
            self.menu.append(refresh_item)

            self.menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", lambda *_: Gtk.main_quit())
            self.menu.append(quit_item)

            self.menu.show_all()
            try:
                self.indicator.set_secondary_activate_target(graph_item)
            except (AttributeError, TypeError):
                pass

    # No variable holds the instance -- `GLib.timeout_add_seconds` keeps it
    # alive via the bound `self._on_timer` reference, for the life of the
    # process (same reasoning as soak_tray.py).
    PresenceTray()

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
