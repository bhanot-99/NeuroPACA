# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · the mirror — the daemon-side driver (VISION_PHASES.md §3.8).

`core/mirror.py` is the pure math; `MirrorComposer(BaseModule)` is the piece
that decides *when* to run it and turns a surprising `MirrorResult` into a
`Moment`'s sentences. Two paths out, the same shape as `BriefingComposer`:
- **proactive** — the day's first `IDLE_DETECTED` at or after
  `mirror_evening_hour` runs `compute_mirror` over today (midnight to now)
  against the trailing baseline; a surprising result becomes a
  `MOMENT_PROPOSED` — A3's `Guardian` decides whether it is ever delivered.
- **on demand** (`neuropaca mirror`) — L9 cannot import this module
  (rules.md §0), so it asks over the bus: `MIRROR_REQUEST` in,
  `MIRROR_REPORT` out — and answers even when there is nothing surprising to
  say, exactly `BRIEFING_REQUEST`'s contract, so L9 never times out waiting.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.mirror import MirrorResult, compute_mirror
from neuropaca.core.models import Event, Moment, system_error_event

_log = logging.getLogger(__name__)

_MOMENT_EXPIRES_MINUTES = 120  # the evening moment stays relevant until bed, not just an hour


def render_mirror(gm: GraphMemory, result: MirrorResult) -> str:
    """§3.8's examples, rendered from evidence: "You spent 2 h more in VS Code
    than a usual Thursday." for a `top` bucket where today ran over the
    baseline; "You didn't open Obsidian today — unusual for you." for a
    `missing` one. Every sentence names a real graph node's display name —
    never a raw subject id — the same grounding discipline as the briefing."""
    sentences: list[str] = []
    for (subject, _hour), _term in result.top:
        sentences.append(f"You spent unusual time in {gm.display_name(subject)} today.")
    for (subject, _hour), _mass in result.missing:
        sentences.append(f"You didn't open {gm.display_name(subject)} today — unusual for you.")
    return " ".join(sentences)


def _evidence(result: MirrorResult) -> tuple[str, ...]:
    subjects = [subject for (subject, _hour), _term in (*result.top, *result.missing)]
    return tuple(dict.fromkeys(subjects))


async def build_mirror_moment(
    gm: GraphMemory,
    store: EpisodeStore,
    *,
    now: datetime,
    config: Config,
) -> Moment | None:
    """The whole pipeline: fetch today + baseline history, run `compute_mirror`,
    render. `None` when the day was not surprising (or there is no baseline
    yet) — F1's grounding rule holds here too: every sentence's subject
    already had to be a real episode-log subject to appear in `top`/`missing`,
    but a subject dropped from the live graph since (renamed, forgotten)
    still resolves through `display_name`'s own fallback, never a raise."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lookback = timedelta(days=config.mirror_baseline_days + 1)
    rows = await store.between(day_start - lookback, now)
    result = compute_mirror(
        rows,
        day_start,
        baseline_days=config.mirror_baseline_days,
        half_life_days=config.mirror_baseline_half_life_days,
        same_weekday_boost=config.mirror_same_weekday_boost,
        threshold=config.mirror_kl_threshold,
        top_n=config.mirror_top_contributors,
    )
    if not result.surprising:
        return None
    text = render_mirror(gm, result)
    if not text:
        return None
    return Moment(
        kind="mirror",
        text=text,
        evidence=_evidence(result),
        value=result.kl,
        context={
            "kl": result.kl,
            "buckets": [
                f"{subject}@{hour}" for (subject, hour), _score in (*result.top, *result.missing)
            ],
        },
        expires_at=now + timedelta(minutes=_MOMENT_EXPIRES_MINUTES),
    )


class MirrorComposer(BaseModule):
    """The daemon-side driver over the pure `compute_mirror` pipeline above."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        episode_store: EpisodeStore,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("mirror", event_bus, config)
        self._graph = graph_memory
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()
        self._last_mirror_date: date | None = None
        self._composed = 0
        self._not_surprising = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.MIRROR_REQUEST, self.on_mirror_request)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.MIRROR_REQUEST, self.on_mirror_request)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._composed} composed · {self._not_surprising} not-surprising · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_idle_detected(self, _event: Event) -> None:
        """The proactive path: the day's *first* idle spell at or after
        `mirror_evening_hour` (config) runs the mirror. Every idle spell
        before that hour, and every one after the first that already
        *succeeded* today, is a no-op — `_last_mirror_date` is only ever
        advanced once the pipeline actually returns, not before the attempt:
        a transient `EpisodeStore` read failure must retry on the next idle
        spell rather than silently costing the user the rest of the day."""
        try:
            if not self.is_running or not self.config.mirror_enabled:
                return
            now = self._clock.now()
            if now.hour < self.config.mirror_evening_hour:
                return
            if self._last_mirror_date == now.date():
                return
            moment = await build_mirror_moment(
                self._graph, self._store, now=now, config=self.config
            )
            self._last_mirror_date = now.date()
            if moment is None:
                self._not_surprising += 1
                return
            self._composed += 1
            self._last_at = now
            self.event_bus.publish(
                Event(
                    event_type=EventType.MOMENT_PROPOSED,
                    source="mirror",
                    payload={"moment": moment},
                )
            )
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_mirror_request(self, event: Event) -> None:
        """The on-demand path (`neuropaca mirror`): compose fresh, right now,
        regardless of the hour or whether today already fired proactively."""
        try:
            if not self.is_running:
                return
            request_id = event.payload.get("request_id")
            now = self._clock.now()
            moment = await build_mirror_moment(
                self._graph, self._store, now=now, config=self.config
            )
            self.event_bus.publish(
                Event(
                    event_type=EventType.MIRROR_REPORT,
                    source="mirror",
                    payload={"request_id": request_id, "moment": moment},
                )
            )
        except Exception as exc:
            self._on_error(exc)

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("mirror handler failed")
        self.event_bus.publish(
            system_error_event(module="mirror", exception=str(exc), severity="handler")
        )
