# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · `PresenceTracker` — the tray's state machine, minus L9 (VISION_PHASES.md).

`interface/layer.py` used to track these five inputs and answer a `presence`
socket op with them. That whole module was removed (no terminal/CLI control
surface — a future voice interface is the planned replacement); this is the
part of it worth keeping regardless: a small always-on subscriber, no socket,
no write-back, that computes `core/presence.py`'s state machine from events
the bus already carries and reports it through the normal `health()` path —
so it shows up in `orchestration/orchestrator.py`'s periodic health-dump file
(`config.health_dump_path`) exactly like every other module's counters.
`scripts/neuropaca_tray.py` reads it from there.

`detail` is a small `key=value` string, the same convention every other
module's `health()` already uses (`scripts/soak_probe.py` parses those with a
permissive regex) — `state=thinking since=2026-09-12T10:00:00+05:30`.
"""

from __future__ import annotations

from datetime import datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, PresenceState
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment
from neuropaca.core.presence import PresenceInputs, compute_presence_state


class PresenceTracker(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("presence", event_bus, config)
        self._clock: Clock = clock or SystemClock()
        self._awake_since: datetime = self._clock.now()
        self._dmn_thinking = False
        self._dmn_thinking_since: datetime | None = None
        self._idle_since: datetime | None = None
        self._focused_since: datetime | None = None
        self._last_moment: Moment | None = None
        self._last_moment_at: datetime | None = None
        self._last_insight_at: datetime | None = None
        self._errors = 0

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.subscribe(EventType.DMN_CYCLE_STARTED, self.on_dmn_cycle_started)
        self.event_bus.subscribe(EventType.DMN_CYCLE_ENDED, self.on_dmn_cycle_ended)
        self.event_bus.subscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)

    async def start(self) -> None:
        self._awake_since = self._clock.now()
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.unsubscribe(EventType.DMN_CYCLE_STARTED, self.on_dmn_cycle_started)
        self.event_bus.unsubscribe(EventType.DMN_CYCLE_ENDED, self.on_dmn_cycle_ended)
        self.event_bus.unsubscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        self.event_bus.unsubscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)

    def health(self) -> ModuleHealth:
        state, since = self._compute()
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=f"state={state.value} since={since.isoformat()} errors={self._errors}",
            last_event_at=since,
        )

    # --------------------------------------------------------------- events
    async def on_app_switch(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            if self._idle_since is None:  # a switch during an open idle spell is not a return
                self._focused_since = self._clock.now()
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_idle_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = self._clock.now()
            self._focused_since = None
        except Exception as exc:
            self._on_error(exc)

    async def on_activity_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = None
            self._focused_since = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    async def on_dmn_cycle_started(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._dmn_thinking = True
            self._dmn_thinking_since = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    async def on_dmn_cycle_ended(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._dmn_thinking = False
        except Exception as exc:
            self._on_error(exc)

    async def on_moment_proposed(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            moment = event.payload.get("moment")
            if isinstance(moment, Moment):
                self._last_moment = moment
                self._last_moment_at = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    async def on_insight_generated(self, _event: Event) -> None:
        """Unlike the old L9's `insights` verb, no confidence/category filter
        and no daily cap — those existed for a surfaced-insights menu this
        tracker does not have. "Noticed something" just needs the fact of a
        new insight, not which ones are worth a human's attention."""
        try:
            if not self.is_running:
                return
            self._last_insight_at = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1

    # ------------------------------------------------------------------ read
    def _compute(self) -> tuple[PresenceState, datetime]:
        now = self._clock.now()
        return compute_presence_state(
            PresenceInputs(
                now=now,
                awake_since=self._awake_since,
                dmn_thinking=self._dmn_thinking,
                dmn_thinking_since=self._dmn_thinking_since,
                last_moment_at=self._last_moment_at,
                last_insight_at=self._last_insight_at,
                idle_since=self._idle_since,
                focused_since=self._focused_since,
            )
        )
