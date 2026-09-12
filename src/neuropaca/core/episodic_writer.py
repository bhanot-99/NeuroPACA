# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `EpisodicWriter` — the only module that writes to the `EpisodeStore`
(VISION_PHASES.md).

Every other module already publishes what it did on the bus; this module's
only job is to turn those events into rows in the durable log, so nothing
upstream needs to import `core/episodes.py` (rules.md §0 — no module imports
another; the bus is the only channel).

- **focus spans** — open on `APP_SWITCH` (remembering the subject and start
  time), closed the moment the *next* `APP_SWITCH` or `IDLE_DETECTED` arrives.
  A switch with an unresolvable subject (no `app_id`, no `webapp`) still
  closes whatever was open — it just does not open a new one. Each span's
  `object` carries the domain it was classified into at focus time — the same
  `AppMap` lookup `SignalCorrelator._classify_into_graph` uses for a bare app,
  `webapp_domain` for a tab — and, for a webapp span, `attrs["browser"]` names
  its browser's own node id. `core/graph_rebuild.py` reads both back to
  recreate the `domain:*` / browser `PART_OF` structure a rebuild used to
  miss (RESEARCH_DOSSIER.md §21.15's "what is left" — now closed).
- **idle spans** — open on `IDLE_DETECTED`, closed on `ACTIVITY_DETECTED`
  (`idle_seconds` on the payload lets the span reproduce even if this module
  missed the original `IDLE_DETECTED` — the fallback path in
  `on_activity_detected`).
- **insights and idle thoughts** — one row per `INSIGHT_GENERATED`, keyed by
  the node id L4/L6 minted for it.
- **delivered moments and their feedback (F2)** — `MOMENT_DELIVERED` /
  `MOMENT_FEEDBACK` are defined in `EventType` now though nothing publishes
  them until A3 exists (the guardian) and F2 (feedback capture); subscribed
  here so the day they start firing needs no change to this file.

A handler never lets an exception escape (rules.md §2) — `record_span` /
`assert_fact` themselves cannot raise (they only enqueue), so the only things
that can go wrong here are a malformed payload or `self._clock`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment, system_error_event
from neuropaca.diagnosis.app_identity import AppIdentity
from neuropaca.diagnosis.app_map import AppMap
from neuropaca.learning.insight import Insight

_log = logging.getLogger(__name__)


class EpisodicWriter(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        episode_store: EpisodeStore,
        *,
        clock: Clock | None = None,
        identity: AppIdentity | None = None,
        app_map: AppMap | None = None,
    ) -> None:
        super().__init__("episodic_writer", event_bus, config)
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()
        self._identity_path = config.app_identity_path
        self._identity = identity if identity is not None else AppIdentity.empty()
        self._app_map_path = config.app_map_path
        self._app_map = app_map if app_map is not None else AppMap.empty()
        self._app_map_provided = app_map is not None
        self._focus_subject: str | None = None
        self._focus_start: datetime | None = None
        self._focus_domain: str | None = None
        self._focus_browser: str | None = None
        self._idle_start: datetime | None = None
        self._spans_written = 0
        self._facts_written = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        if self._identity.alias_count == 0:
            self._identity = AppIdentity.from_file(self._identity_path)
        if not self._app_map_provided:
            self._app_map = AppMap.from_file(self._app_map_path)
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)
        self.event_bus.subscribe(EventType.MOMENT_DELIVERED, self.on_moment_delivered)
        self.event_bus.subscribe(EventType.MOMENT_FEEDBACK, self.on_moment_feedback)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.unsubscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)
        self.event_bus.unsubscribe(EventType.MOMENT_DELIVERED, self.on_moment_delivered)
        self.event_bus.unsubscribe(EventType.MOMENT_FEEDBACK, self.on_moment_feedback)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._spans_written} span(s) · {self._facts_written} fact(s) · "
                f"{self._store.queue_depth} queued · {self._store.dropped_count} dropped · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_app_switch(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            now = self._clock.now()
            self._close_focus_span(now)
            payload = event.payload
            raw_app_id = payload.get("app_id")
            app_id = raw_app_id if isinstance(raw_app_id, str) and raw_app_id else None
            browser_id = f"app:{self._identity.resolve(app_id) or app_id}" if app_id else None

            webapp = payload.get("webapp")
            if isinstance(webapp, str) and webapp:
                raw_wdom = payload.get("webapp_domain")
                self._focus_subject = f"webapp:{webapp}"
                self._focus_domain = raw_wdom if isinstance(raw_wdom, str) and raw_wdom else None
                self._focus_browser = browser_id
                self._focus_start = now
            elif browser_id is not None and app_id is not None:
                self._focus_subject = browser_id
                self._focus_domain = self._app_map.classify(app_id)
                self._focus_browser = None
                self._focus_start = now
            self._last_at = now
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_idle_detected(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            now = self._clock.now()
            self._close_focus_span(now)
            self._idle_start = now
            self._last_at = now
        except Exception as exc:
            self._on_error(exc)

    async def on_activity_detected(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            now = self._clock.now()
            start = self._idle_start
            if start is None:
                idle_seconds = float(event.payload.get("idle_seconds", 0.0))
                if idle_seconds > 0.0:
                    from datetime import timedelta

                    start = now - timedelta(seconds=idle_seconds)
            if start is not None and now > start:
                self._store.record_span(EpisodeKind.IDLE_SPAN, "YOU", start, now)
                self._spans_written += 1
            self._idle_start = None
            self._last_at = now
        except Exception as exc:
            self._on_error(exc)

    async def on_insight_generated(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            insight = event.payload.get("insight")
            if not isinstance(insight, Insight):
                return
            subject = insight.node_id or f"insight:{insight.category}"
            self._store.record_span(
                EpisodeKind.INSIGHT,
                subject,
                insight.created_at,
                insight.created_at,
                obj=insight.category,
                source=event.source,
                attrs={"label": insight.label, "cited": list(insight.cited_node_ids)},
            )
            self._spans_written += 1
            self._last_at = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    async def on_moment_delivered(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._record_moment_episode(event, EpisodeKind.MOMENT_DELIVERED)
        except Exception as exc:
            self._on_error(exc)

    async def on_moment_feedback(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._record_moment_episode(event, EpisodeKind.MOMENT_FEEDBACK)
        except Exception as exc:
            self._on_error(exc)

    def _record_moment_episode(self, event: Event, kind: EpisodeKind) -> None:
        moment = event.payload.get("moment")
        now = self._clock.now()
        if not isinstance(moment, Moment):
            return
        outcome = event.payload.get("outcome")
        self._store.record_span(
            kind,
            f"moment:{moment.kind}",
            now,
            now,
            obj=str(outcome) if outcome is not None else None,
            source=event.source,
            attrs={"text": moment.text, "evidence": list(moment.evidence)},
        )
        self._spans_written += 1
        self._last_at = now

    def _close_focus_span(self, now: datetime) -> None:
        subject, start = self._focus_subject, self._focus_start
        if subject is not None and start is not None and now > start:
            attrs = {"browser": self._focus_browser} if self._focus_browser else {}
            self._store.record_span(
                EpisodeKind.FOCUS_SPAN, subject, start, now, obj=self._focus_domain, attrs=attrs
            )
            self._spans_written += 1
        self._focus_subject = None
        self._focus_start = None
        self._focus_domain = None
        self._focus_browser = None

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("episodic_writer handler failed")
        self.event_bus.publish(
            system_error_event(module="episodic_writer", exception=str(exc), severity="handler")
        )


# gen-ref: 6a1d4e79
