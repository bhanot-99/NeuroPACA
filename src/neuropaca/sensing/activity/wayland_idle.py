"""`WaylandIdleSource` — idle/activity from `ext-idle-notify-v1` (B2.5, D-9).

Spike-verified on cosmic-comp (`spikes/b2_5_activity/`). pywayland is an optional
dependency (`pip install .[activity]`) and is imported lazily inside `start()`; a
missing library, no `$WAYLAND_DISPLAY`, or a compositor without the protocol all
raise `CollectorError`, which `ActivityCollector` turns into a graceful
self-disable — never a crash (rules.md §2).

Event-loop integration: the compositor socket fd is registered with
`loop.add_reader` and drained with `Display.read()` + `Display.dispatch()`. One
thread, one loop, no locks (rules.md §3).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.activity.idle import IdleCallback, IdleTransition

_IDLE_NOTIFY_MAX_VERSION = 2
_SEAT_MAX_VERSION = 4
_MIN_TIMEOUT_MS = 1000


class WaylandIdleSource:
    def __init__(self, idle_threshold_seconds: int) -> None:
        self._timeout_ms = max(_MIN_TIMEOUT_MS, idle_threshold_seconds * 1000)
        self._display: Any = None
        self._notification: Any = None
        self._fd = -1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cb: IdleCallback | None = None

    def start(self, on_transition: IdleCallback) -> None:
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise CollectorError("no $WAYLAND_DISPLAY — not a Wayland session")
        try:
            from pywayland.client import Display
            from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
            from pywayland.protocol.wayland import WlSeat
        except ImportError as exc:
            raise CollectorError(
                f"pywayland not installed (pip install .[activity]): {exc}"
            ) from exc

        self._cb = on_transition
        self._loop = asyncio.get_running_loop()

        try:
            display = Display()
            display.connect()
        except Exception as exc:  # pywayland raises bare exceptions on connect failure
            raise CollectorError(f"cannot connect to the Wayland display: {exc}") from exc

        found: dict[str, Any] = {}
        registry = display.get_registry()

        def _on_global(_reg: Any, name: int, interface: str, version: int) -> None:
            if interface == "ext_idle_notifier_v1":
                found["notifier"] = registry.bind(
                    name, ExtIdleNotifierV1, min(version, _IDLE_NOTIFY_MAX_VERSION)
                )
            elif interface == "wl_seat":
                found["seat"] = registry.bind(name, WlSeat, min(version, _SEAT_MAX_VERSION))

        registry.dispatcher["global"] = _on_global
        display.roundtrip()

        if "notifier" not in found or "seat" not in found:
            display.disconnect()
            raise CollectorError("compositor does not offer ext_idle_notifier_v1 + wl_seat")

        notification = found["notifier"].get_idle_notification(self._timeout_ms, found["seat"])
        notification.dispatcher["idled"] = self._on_idled
        notification.dispatcher["resumed"] = self._on_resumed

        self._display = display
        self._notification = notification
        self._fd = display.get_fd()
        display.flush()
        self._loop.add_reader(self._fd, self._on_readable)

    # ---- dispatcher callbacks (must return None — pywayland cffi contract) ----
    def _on_idled(self, _notification: Any) -> None:
        if self._cb is not None:
            self._cb(IdleTransition.IDLE)

    def _on_resumed(self, _notification: Any) -> None:
        if self._cb is not None:
            self._cb(IdleTransition.ACTIVE)

    def _on_readable(self) -> None:
        display = self._display
        if display is None:
            return
        try:
            display.read()
            display.dispatch(block=False)
            display.flush()
        except Exception:
            # broken connection — fail safe to ACTIVE and stop watching
            cb = self._cb
            self.stop()
            if cb is not None:
                cb(IdleTransition.ACTIVE)

    def stop(self) -> None:
        if self._loop is not None and self._fd >= 0:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, OSError):
                pass
        self._fd = -1
        if self._display is not None:
            try:
                self._display.disconnect()
            except Exception:
                pass
        self._display = None
        self._notification = None
        self._cb = None
