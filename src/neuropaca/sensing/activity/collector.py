"""`ActivityCollector` — real idle/activity edges (B2.5, D-9).

A BaseModule (not a polled BaseCollector — idle-notify is event-driven). It owns
an `IdleSource` and republishes its transitions as `IDLE_DETECTED` /
`ACTIVITY_DETECTED`, the signal that replaces `XMetricCollector`'s CPU-derived
stand-in (D-7 A3). If the source cannot start (no pywayland, headless, wrong
compositor) the module stays "running" but inert and publishes one
`SYSTEM_ERROR` — the rest of the daemon is unaffected (rules.md §2).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import CollectorError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.activity.idle import IdleSource, IdleTransition

_log = logging.getLogger(__name__)


class ActivityCollector(BaseModule):
    def __init__(
        self, event_bus: EventBus, config: Config, *, idle_source: IdleSource | None = None
    ) -> None:
        super().__init__("activity", event_bus, config)
        self._idle_threshold = config.idle_threshold_seconds
        self._source: IdleSource | None = idle_source
        self._enabled = False
        self._idle = False
        self._idle_since: datetime | None = None
        self._transitions = 0

    async def initialize(self) -> None:
        return None  # publishes only; subscribes to nothing

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        if self._source is None:
            from neuropaca.sensing.activity.wayland_idle import WaylandIdleSource

            self._source = WaylandIdleSource(self._idle_threshold)
        try:
            self._source.start(self._on_transition)
        except CollectorError as exc:
            self._enabled = False
            _log.warning("ActivityCollector disabled: %s", exc)
            self.event_bus.publish(
                system_error_event(
                    module="sensing.activity", exception=str(exc), severity="collector-disabled"
                )
            )
            return
        self._enabled = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        if self._enabled and self._source is not None:
            self._source.stop()
        self._enabled = False

    def health(self) -> ModuleHealth:
        detail = "wayland idle-notify" if self._enabled else "inert — no idle source"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=f"{detail} · {self._transitions} transitions",
            last_event_at=self._idle_since,
        )

    # -------------------------------------------------------- source callback
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
