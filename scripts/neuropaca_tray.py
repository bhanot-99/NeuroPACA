#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the L9 tray — a permanent presence (VISION_PHASES.md, "Living presence").

WHAT THIS IS

You can glance at the corner of your screen and see that NeuroPACA is there
and what it is doing — focused, idle, thinking, or "noticed something". One
click shows today's idle thoughts; another opens the graph view; another asks
"what changed today" (A2's mirror, on demand — this item was deferred out of
A1's own first cut, since the mirror did not exist yet); another gives
feedback on the last thing it said.

NOT `scripts/soak_tray.py`

That script is a throwaway B9-hardening diagnostic for one 7-day soak — it
reads two files off disk, no daemon coupling. This is the permanent L9
surface: it talks to the daemon over its own socket (`presence` / `pause` /
`feedback`), and it is meant to outlive any one soak. The two scripts do not
import each other, on purpose — `soak_tray.py`'s docstring says to delete it
once its soak completes, and this one must not go down with it.

PURE LOGIC VS. TOOLKIT GLUE

`request()`, `compute_tray_view()`, `menu_header()`, `thought_lines()` below
are plain functions over stdlib types — no `gi` import, so they run and are
unit-tested (`tests/test_neuropaca_tray.py`) under the project .venv, which
deliberately has no PyGObject. GTK/AppIndicator only enter in `_run_tray()`,
imported lazily — that half is verified live on this machine, not by pytest.
Same split `soak_tray.py` established; see its own docstring for the fuller
reasoning (the lazy `pywayland` import in `sensing/activity/wayland_idle.py`
is the same idea one layer down).

WHY SYSTEM PYTHON, NOT THE PROJECT .venv

PyGObject + AyatanaAppIndicator3 are desktop-shell bindings (apt package:
gir1.2-ayatanaappindicator3-0.1), not a neuropaca runtime dependency, and the
project .venv is built with --system-site-packages off:

    scripts/neuropaca_tray.py &

or enable scripts/systemd/neuropaca-tray.service — the user's call, per the
phase design; nothing installs or starts it automatically.

POLL, NOT PUSH

L9 is a request/response socket, not a push channel — the tray asks for
`presence` every `POLL_SECONDS` (5s, per the phase's own spike question: "is
a 5s poll invisible in CPU? Expected yes") rather than the daemon pushing
updates. A local AF_UNIX round trip at that cadence is not measurable CPU.

SURVIVING A RESTART

`request()` returns `None` on *any* failure — no socket, connection refused,
timeout, malformed reply — and `compute_tray_view(None)` renders that as
"asleep", never a crash and never a stale "focused" icon frozen from before
the daemon went away. The exit criterion this satisfies: the tray survives
the daemon restarting.
"""

from __future__ import annotations

import json
import os
import shutil
import socket as socket_lib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
GRAPH_RENDER = REPO / "scripts" / "neuropaca_graph.py"
GRAPH_HTML = REPO / "data" / "graph_view.html"

POLL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 2.0
PAUSE_MINUTES = 60.0
_MAX_THOUGHT_ROWS = 5

ICON_THINKING = "view-refresh"
ICON_NOTICED = "mail-unread"
ICON_FOCUSED = "user-available"
ICON_IDLE = "user-idle"
ICON_AWAKE = "user-available"
ICON_ASLEEP = "user-offline"  # the daemon is unreachable — not one of the five real states
ICON_ERROR = "dialog-error"

# The widest label this tray ever actually shows ("Thinking", 8 chars) — the
# fixed sizing hint AppIndicator3.set_label's second argument wants, so the
# panel does not resize on every state change. An earlier version hardcoded
# "Focused" here regardless of state — a leftover copy from soak_tray.py's
# own percentage-width guide, unrelated to this tray's label vocabulary.
LABEL_WIDTH_GUIDE = "Thinking"

_ICON_BY_STATE = {
    "thinking": ICON_THINKING,
    "noticed": ICON_NOTICED,
    "focused": ICON_FOCUSED,
    "idle": ICON_IDLE,
    "awake": ICON_AWAKE,
}

# Same fallback chain as soak_tray.py, duplicated rather than imported —
# see the module docstring for why the two scripts stay independent.
BRAVE_BINARIES = ("brave-browser", "brave", "brave-browser-stable")


def default_socket_path() -> Path:
    override = os.environ.get("NEUROPACA_SOCKET")
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "neuropaca.sock"


def request(
    sock_path: Path, payload: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> dict[str, Any] | None:
    """One JSONL request/response over the L9 socket. `None` on *any* failure
    — no socket, refused, timeout, malformed reply — so the caller treats
    every failure mode uniformly as "the daemon is asleep", never a crash."""
    sock: socket_lib.socket | None = None
    try:
        sock = socket_lib.socket(socket_lib.AF_UNIX, socket_lib.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while b"\n" not in b"".join(chunks):
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        line = b"".join(chunks).split(b"\n", 1)[0]
        if not line:
            return None
        parsed = json.loads(line)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if sock is not None:
            sock.close()


@dataclass(frozen=True, slots=True)
class TrayView:
    """Everything one refresh needs to render. Plain data — nothing here
    touches a GTK object, which is what makes it constructible in a test
    without gi."""

    state: str  # one of the five presence states, or "asleep" / "error"
    icon_name: str
    label: str
    thoughts_today: tuple[str, ...]
    last_moment_text: str | None
    paused: bool
    error: str | None


def compute_tray_view(presence: dict[str, Any] | None) -> TrayView:
    """Pure: no socket, no gi. `presence` is exactly `request()`'s return."""
    if presence is None:
        return TrayView(
            state="asleep",
            icon_name=ICON_ASLEEP,
            label="Asleep",
            thoughts_today=(),
            last_moment_text=None,
            paused=False,
            error="daemon unreachable",
        )
    if not presence.get("ok"):
        return TrayView(
            state="error",
            icon_name=ICON_ERROR,
            label="Error",
            thoughts_today=(),
            last_moment_text=None,
            paused=False,
            error=str(presence.get("error", "unknown error")),
        )

    state = str(presence.get("state", "awake"))
    last_moment = presence.get("last_moment")
    thoughts = presence.get("thoughts_today")
    return TrayView(
        state=state,
        icon_name=_ICON_BY_STATE.get(state, ICON_AWAKE),
        label=state.capitalize(),
        thoughts_today=tuple(str(t.get("text", "")) for t in thoughts if isinstance(t, dict))
        if isinstance(thoughts, list)
        else (),
        last_moment_text=str(last_moment.get("text", ""))
        if isinstance(last_moment, dict)
        else None,
        paused=bool(presence.get("paused_until")),
        error=None,
    )


def menu_header(view: TrayView) -> str:
    if view.error:
        return f"NeuroPACA · {view.label} ({view.error})"
    paused_note = " · paused" if view.paused else ""
    return f"NeuroPACA · {view.label}{paused_note}"


def thought_lines(view: TrayView, limit: int = _MAX_THOUGHT_ROWS) -> list[str]:
    """The newest `limit` idle thoughts from today, newest last — same order
    they were noticed in, capped so the menu never runs off the screen."""
    return list(view.thoughts_today[-limit:])


def mirror_summary_text(resp: dict[str, Any] | None) -> str:
    """A2's on-demand `mirror` op, summarised for the "What changed today"
    menu item's dialog — A1's own deferral note ("its backend, the mirror,
    does not exist yet") no longer applies, so this is that item, finally
    built. Every failure mode (no daemon, no live `MirrorComposer` because
    episodes are disabled, nothing surprising today) renders as a plain
    sentence, the same shape `interface/cli.py`'s `mirror` op already prints
    — never a raised error reaching the dialog."""
    if resp is None:
        return "Couldn't reach the daemon."
    if not resp.get("ok"):
        return str(resp.get("error", "Couldn't reach the daemon."))
    moment = resp.get("moment")
    if not isinstance(moment, dict) or not moment.get("text"):
        return "Nothing unusual today."
    return str(moment["text"])


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

    sock_path = default_socket_path()

    class PresenceTray:
        def __init__(self) -> None:
            self.indicator = AppIndicator3.Indicator.new(
                "neuropaca",
                ICON_ASLEEP,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.view = compute_tray_view(None)
            self.menu = Gtk.Menu()
            self.indicator.set_menu(self.menu)
            self.refresh()
            GLib.timeout_add_seconds(POLL_SECONDS, self._on_timer)

        def _on_timer(self) -> bool:
            self.refresh()
            return True  # GLib.SOURCE_CONTINUE

        def refresh(self) -> None:
            presence = request(sock_path, {"op": "presence"})
            self.view = compute_tray_view(presence)
            self.indicator.set_icon_full(self.view.icon_name, self.view.label)
            self.indicator.set_label(self.view.label, LABEL_WIDTH_GUIDE)
            self._populate_menu()

        def _on_refresh_clicked(self, *_args: object) -> None:
            # Same reasoning as soak_tray.py's own `_on_refresh_clicked`: hand
            # the rebuild to the next idle turn so the menu is not torn down
            # from inside its own item's `activate` emission.
            GLib.idle_add(self._refresh_once)

        def _refresh_once(self) -> bool:
            self.refresh()
            return GLib.SOURCE_REMOVE

        def _on_pause_clicked(self, *_args: object) -> None:
            request(sock_path, {"op": "pause", "minutes": PAUSE_MINUTES})
            GLib.idle_add(self._refresh_once)

        def _on_feedback_clicked(self, outcome: str) -> None:
            request(sock_path, {"op": "feedback", "outcome": outcome})

        def _on_mirror_clicked(self, *_args: object) -> None:
            # A2's "what did you learn today" (A1's own deferred menu item,
            # now that the mirror exists to back it). Same idle_add hand-off
            # as `_on_refresh_clicked` — never tear down the menu from inside
            # its own item's `activate` emission — and the request itself is
            # a bounded, one-shot round trip, not the 5 s poll loop.
            GLib.idle_add(self._show_mirror_once)

        def _show_mirror_once(self) -> bool:
            # `mirror` composes a KL divergence over up to 14 days of history
            # server-side (`_MIRROR_TIMEOUT` in interface/layer.py is 6.0 s) —
            # this client timeout needs margin over that, not to sit at or
            # below it (the exact race `_BRIEFING_TIMEOUT`/`_MIRROR_TIMEOUT`
            # were themselves sized against in the test harness).
            resp = request(sock_path, {"op": "mirror"}, timeout=8.0)
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="What changed today",
            )
            dialog.format_secondary_text(mirror_summary_text(resp))
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.show()
            return GLib.SOURCE_REMOVE

        def _populate_menu(self) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)
            view = self.view

            header = Gtk.MenuItem(label=menu_header(view))
            header.set_sensitive(False)
            self.menu.append(header)
            self.menu.append(Gtk.SeparatorMenuItem())

            graph_item = Gtk.MenuItem(label="Open graph view")
            graph_item.connect("activate", lambda *_: open_graph_view())
            self.menu.append(graph_item)

            mirror_item = Gtk.MenuItem(label="What changed today")
            mirror_item.connect("activate", self._on_mirror_clicked)
            self.menu.append(mirror_item)
            self.menu.append(Gtk.SeparatorMenuItem())

            lines = thought_lines(view)
            if lines:
                thoughts_header = Gtk.MenuItem(label="Today's thoughts")
                thoughts_header.set_sensitive(False)
                self.menu.append(thoughts_header)
                for line in lines:
                    item = Gtk.MenuItem(label=f"  {line}")
                    item.set_sensitive(False)
                    self.menu.append(item)
                self.menu.append(Gtk.SeparatorMenuItem())

            if view.last_moment_text:
                moment_header = Gtk.MenuItem(label=f'Last: "{view.last_moment_text}"')
                moment_header.set_sensitive(False)
                self.menu.append(moment_header)
                keep_item = Gtk.MenuItem(label="\N{THUMBS UP SIGN} Keep")
                keep_item.connect("activate", lambda *_: self._on_feedback_clicked("accepted"))
                self.menu.append(keep_item)
                dismiss_item = Gtk.MenuItem(label="\N{THUMBS DOWN SIGN} Dismiss")
                dismiss_item.connect("activate", lambda *_: self._on_feedback_clicked("dismissed"))
                self.menu.append(dismiss_item)
                self.menu.append(Gtk.SeparatorMenuItem())

            pause_label = "Paused (1h)" if view.paused else "Pause for 1 hour"
            pause_item = Gtk.MenuItem(label=pause_label)
            pause_item.set_sensitive(not view.paused)
            pause_item.connect("activate", self._on_pause_clicked)
            self.menu.append(pause_item)

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
