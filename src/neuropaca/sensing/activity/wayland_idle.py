"""`WaylandIdleSource` — idle/activity from `ext-idle-notify-v1` (B2.5, D-9).

Spike-verified on cosmic-comp (`spikes/b2_5_activity/`). B15: a **protocol
handler on a shared `WaylandConnection`** — it owns no `Display` (two `Display`
connections in one process makes one go deaf; see `wayland_conn.py`). Given no
connection it creates a private one; the `ActivityCollector` passes in the
connection it also shares with `WaylandWindowSource`.

pywayland is an optional dependency (`pip install .[activity]`), lazy-imported; a
missing library, no `$WAYLAND_DISPLAY`, or a compositor without the protocol all
raise `CollectorError`, which `ActivityCollector` turns into a graceful
self-disable — never a crash (rules.md §2).
"""

from __future__ import annotations

import logging
from typing import Any

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.activity.idle import IdleCallback, IdleTransition
from neuropaca.sensing.activity.wayland_conn import WaylandConnection

_log = logging.getLogger(__name__)

_IDLE_NOTIFY_MAX_VERSION = 2
_SEAT_MAX_VERSION = 4
_MIN_TIMEOUT_MS = 1000


class WaylandIdleSource:
    def __init__(
        self, idle_threshold_seconds: int, *, connection: WaylandConnection | None = None
    ) -> None:
        self._timeout_ms = max(_MIN_TIMEOUT_MS, idle_threshold_seconds * 1000)
        self._notifier: Any = None
        self._seat: Any = None
        self._notification: Any = None
        self._cb: IdleCallback | None = None
        self._owns_connection = connection is None
        self._conn = connection if connection is not None else WaylandConnection()
        self._conn.add(self)

    def start(self, on_transition: IdleCallback) -> None:
        self._cb = on_transition
        if self._owns_connection:
            self._conn.start()  # synchronous — raises CollectorError to self-disable

    @property
    def is_alive(self) -> bool:
        return self._conn.is_alive

    # ------------------------------------------- WaylandConnection protocol hooks
    def wants(self) -> dict[str, tuple[type, int]]:
        from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
        from pywayland.protocol.wayland import WlSeat

        return {
            "ext_idle_notifier_v1": (ExtIdleNotifierV1, _IDLE_NOTIFY_MAX_VERSION),
            "wl_seat": (WlSeat, _SEAT_MAX_VERSION),
        }

    def bound(self, globals_: dict[str, Any]) -> None:
        self._notifier = globals_.get("ext_idle_notifier_v1")
        self._seat = globals_.get("wl_seat")
        if self._notifier is None or self._seat is None:
            raise CollectorError("compositor does not offer ext_idle_notifier_v1 + wl_seat")

    def primed(self) -> None:
        notification = self._notifier.get_idle_notification(self._timeout_ms, self._seat)
        notification.dispatcher["idled"] = self._on_idled
        notification.dispatcher["resumed"] = self._on_resumed
        self._notification = notification

    def lost(self) -> None:
        # a lost idle connection must not leave us wrongly "idle"
        cb = self._cb
        self._notifier = self._seat = self._notification = None
        if cb is not None:
            cb(IdleTransition.ACTIVE)

    # ---- dispatcher callbacks (must return None — pywayland cffi contract) ----
    def _on_idled(self, _notification: Any) -> None:
        if self._cb is not None:
            self._cb(IdleTransition.IDLE)

    def _on_resumed(self, _notification: Any) -> None:
        if self._cb is not None:
            self._cb(IdleTransition.ACTIVE)

    def stop(self) -> None:
        if self._owns_connection:
            self._conn.stop()
        self._cb = None
