"""`ActivityCollector` — real idle / activity / focus edges (B2.5, D-9).

A BaseModule (not a polled BaseCollector — the sources are event-driven). It owns
an `IdleSource` (`IDLE_DETECTED` / `ACTIVITY_DETECTED`, replacing the D-7 A3
CPU-derived stand-in) and, from B2.5b, a `WindowSource` (`APP_SWITCH` on the
focused `app_id` changing). Either source failing to start (no pywayland,
headless, wrong compositor) is logged as one `SYSTEM_ERROR` and that half goes
inert — the module and the rest of the daemon keep running (rules.md §2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import CollectorError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.activity.idle import IdleSource, IdleTransition
from neuropaca.sensing.activity.webapp import WebAppMap, derive_webapp
from neuropaca.sensing.activity.window import WindowInfo, WindowSource

_log = logging.getLogger(__name__)


class ActivityCollector(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        idle_source: IdleSource | None = None,
        window_source: WindowSource | None = None,
    ) -> None:
        super().__init__("activity", event_bus, config)
        self._idle_threshold = config.idle_threshold_seconds
        self._idle_source = idle_source
        self._window_source = window_source
        self._idle_ok = False
        self._window_ok = False
        self._idle = False
        self._idle_since: datetime | None = None
        self._transitions = 0
        self._switches = 0
        # B14 · focus key = (app_id, webapp_label | None). A tab switch inside a
        # browser changes the second half; "Inbox (351)" -> "(352)" does not.
        self._focus_key: tuple[str, str | None] | None = None
        self._webapp_enabled = config.webapp_tracking_enabled
        self._browsers = frozenset(config.webapp_browser_app_ids)
        self._webapp_map_path = config.webapp_map_path
        self._webapp_map = WebAppMap.empty()
        self._webapps_seen: set[str] = set()
        self._wl_conn: Any = None  # the shared WaylandConnection on the real path

    async def initialize(self) -> None:
        return None  # publishes only; subscribes to nothing

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True

        if self._webapp_enabled and self._browsers:
            self._webapp_map = WebAppMap.from_file(self._webapp_map_path)

        if self._idle_source is None and self._window_source is None:
            # Real path: ONE shared Wayland connection carries both protocols
            # (idle-notify + toplevel-info). Two Display connections in one
            # process makes the second go deaf — see wayland_conn.py (B15).
            from neuropaca.sensing.activity.wayland_conn import WaylandConnection
            from neuropaca.sensing.activity.wayland_idle import WaylandIdleSource
            from neuropaca.sensing.activity.window import WaylandWindowSource

            conn = WaylandConnection()
            conn.activity_probe = lambda: not self._idle  # gates the liveness watchdog
            self._idle_source = WaylandIdleSource(self._idle_threshold, connection=conn)
            self._window_source = WaylandWindowSource(
                title_sensitive_app_ids=self._browsers if self._webapp_enabled else frozenset(),
                connection=conn,
            )
            self._idle_source.start(self._on_transition)  # store cb; conn not started yet
            self._window_source.start(self._on_window_switch)
            started = self._try_start("wayland", conn.start)
            self._idle_ok = self._window_ok = started
            self._wl_conn = conn
        else:
            # Injected doubles (tests): each source drives itself.
            idle = self._idle_source
            if idle is not None:
                self._idle_ok = self._try_start("idle", lambda: idle.start(self._on_transition))
            window = self._window_source
            if window is not None:
                self._window_ok = self._try_start(
                    "window", lambda: window.start(self._on_window_switch)
                )

    def _try_start(self, label: str, run: Callable[[], None]) -> bool:
        try:
            run()
        except CollectorError as exc:
            _log.warning("ActivityCollector %s source disabled: %s", label, exc)
            self.event_bus.publish(
                system_error_event(
                    module=f"sensing.activity.{label}",
                    exception=str(exc),
                    severity="collector-disabled",
                )
            )
            return False
        return True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        if self._idle_ok and self._idle_source is not None:
            self._idle_source.stop()
        if self._window_ok and self._window_source is not None:
            self._window_source.stop()
        if self._wl_conn is not None:
            self._wl_conn.stop()  # the real shared connection (source.stop() is a no-op for it)
        self._idle_ok = self._window_ok = False

    def health(self) -> ModuleHealth:
        # B15 · "✓" now means the source is started AND its poll-pump is still
        # live. A pump that died and burned its reconnect budget flips is_alive to
        # False, so health stops printing "✓" for a sensor that has gone deaf
        # (the B7 failure was a dead collector that still looked healthy).
        idle_live = self._idle_ok and self._idle_source is not None and self._idle_source.is_alive
        window_live = (
            self._window_ok and self._window_source is not None and self._window_source.is_alive
        )
        idle = "idle✓" if idle_live else "idle✗"
        window = "window✓" if window_live else "window✗"
        # A source that STARTED and then died drags the module unhealthy — that is
        # the alarm B7 never had. A source that never started (headless, no
        # compositor) stays tolerated, exactly as before.
        died = (self._idle_ok and not idle_live) or (self._window_ok and not window_live)
        return ModuleHealth(
            name=self.name,
            ok=self.is_running and not died,
            detail=f"{idle} {window} · {self._transitions} transitions · {self._switches} switches",
            last_event_at=self._idle_since,
        )

    # ---------------------------------------------------------- source callbacks
    def _on_transition(self, transition: IdleTransition) -> None:
        now = datetime.now(UTC)
        if transition is IdleTransition.IDLE and not self._idle:
            self._idle = True
            self._idle_since = now
            self._transitions += 1
            self.event_bus.publish(
                Event(
                    event_type=EventType.IDLE_DETECTED,
                    source="sensing.activity",
                    payload={"source": "wayland", "idle_seconds": float(self._idle_threshold)},
                )
            )
        elif transition is IdleTransition.ACTIVE and self._idle:
            idle_seconds = (
                (now - self._idle_since).total_seconds()
                if self._idle_since is not None
                else float(self._idle_threshold)
            )
            self._idle = False
            self._idle_since = None
            self._transitions += 1
            self.event_bus.publish(
                Event(
                    event_type=EventType.ACTIVITY_DETECTED,
                    source="sensing.activity",
                    payload={"source": "wayland", "idle_seconds": idle_seconds},
                )
            )

    def _on_window_switch(self, window: WindowInfo) -> None:
        # The raw title is read HERE and nowhere downstream: `derive_webapp`
        # returns only an allowlisted label, and that is all that goes on the bus
        # (B14 — the membrane).
        hit = derive_webapp(
            window.app_id,
            window.title,
            browsers=self._browsers,
            webapp_map=self._webapp_map,
            enabled=self._webapp_enabled,
        )
        label = hit.label if hit is not None else None
        key = (window.app_id, label)
        if key == self._focus_key:
            return
        prev_app_id, prev_label = self._focus_key or (None, None)
        self._focus_key = key
        self._switches += 1
        if label is not None:
            self._webapps_seen.add(label)
        self.event_bus.publish(
            Event(
                event_type=EventType.APP_SWITCH,
                source="sensing.activity",
                payload={
                    "app_id": window.app_id,
                    "webapp": label,
                    "webapp_domain": hit.domain if hit is not None else None,
                    "previous_app_id": prev_app_id,
                    "previous_webapp": prev_label,
                },
            )
        )
