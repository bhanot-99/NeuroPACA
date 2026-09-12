# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L3 · `SignalCorrelator` — the diagnosis module (Architecture.md §5, D-8).

Subscribes to `METRIC_COLLECTED`. For every snapshot it appends to that
collector's bounded deque, updates the per-metric baselines, runs the patterns
that read that collector, and for each firing pattern writes the implicated
nodes and publishes `SIGNAL_CORRELATED` / `MEMORY_UPDATED` / `PATTERN_DETECTED`.

It runs entirely on the `EventBus` dispatch loop. The only `await`s are single
graph mutations — never one lock around the batch (rules.md §3). Pattern
matching is pure CPU. Zero inference in L3 (B3 exit criterion).
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.coactivation import CoactivationWindow
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.diagnosis.app_identity import AppIdentity
from neuropaca.diagnosis.app_map import AppMap
from neuropaca.diagnosis.patterns import BasePattern, build_patterns
from neuropaca.diagnosis.signal import MetricBaseline, Signal, SignalDraft
from neuropaca.sensing.snapshot import MetricSnapshot

_log = logging.getLogger(__name__)

# APP_SWITCH events have no fixed cadence; this nominal interval only bounds the
# synthetic "activity" deque (maxlen = ceil(correlation_window / this) + 1, D-10).
_ACTIVITY_NOMINAL_POLL = 2.0
_ACTIVITY_COLLECTOR = "activity"
_CANON_CACHE_MAX = 1024
# V-10 · a focused node's sighting is written at most this often. A focus storm
# (alt-tab, or a compositor firing focus events) must stay lock-free — V-1 went
# to lengths for that — and "last seen" at minute resolution is all a briefing
# can use. Past `_CANON_CACHE_MAX` entries the gate drops the ones older than
# this window: they would pass the gate anyway, so they carry no information.
_SIGHTING_REFRESH_SECONDS = 60.0


def _now() -> float:
    """Co-activation clock. `CLOCK_BOOTTIME` keeps counting through suspend,
    where `time.monotonic()` stops — so a lid closed for the night is a long gap,
    not an instant one."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


class SignalCorrelator(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        *,
        patterns: Sequence[BasePattern] | None = None,
        identity: AppIdentity | None = None,
    ) -> None:
        super().__init__("diagnosis", event_bus, config)
        self._graph = graph_memory
        self._patterns: list[BasePattern] = (
            list(patterns) if patterns is not None else build_patterns(config)
        )
        self._window_seconds = config.correlation_window_seconds
        self._poll_intervals = dict(config.poll_intervals)
        self._poll_intervals.setdefault(_ACTIVITY_COLLECTOR, _ACTIVITY_NOMINAL_POLL)
        self._app_map_path = config.app_map_path
        self._app_map = AppMap.empty()
        self._identity_path = config.app_identity_path
        self._identity = identity if identity is not None else AppIdentity.empty()
        self._known_apps: set[str] = set()
        self._known_webapps: set[str] = set()
        self._windows: dict[str, deque[MetricSnapshot]] = {}
        self._baselines: dict[tuple[str, str], MetricBaseline] = {}
        self._signals_emitted = 0
        self._errors = 0
        self._last_signal_at: datetime | None = None
        # T7 · Hebbian co-activation. `CoactivationWindow` (core/coactivation.py,
        # S0) is the shared state/math the deterministic graph rebuild also
        # replays, so both apply *the same* "fire together, wire together"
        # rule from *the same* state shape — never two implementations that
        # could quietly drift.
        self._window = CoactivationWindow(
            window_seconds=config.coactivation_window_seconds,
            max_nodes=config.coactivation_max_nodes,
            refractory_seconds=config.coactivation_refractory_seconds,
        )
        self._hebbian_delta = float(config.hebbian_delta)
        self._hebbian_wired = 0
        self._focus_exclude = frozenset(a.lower() for a in config.focus_exclude_app_ids)
        self._canon_cache: dict[str, tuple[str, str]] = {}
        # V-10 · node id -> monotonic time its last sighting was written
        self._sighted_at: dict[str, float] = {}

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self._app_map = AppMap.from_file(self._app_map_path)
        if self._identity.alias_count == 0:
            self._identity = AppIdentity.from_file(self._identity_path)
            self._canon_cache.clear()  # resolved against the old identity
        self.event_bus.subscribe(EventType.METRIC_COLLECTED, self.on_metric_event)
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.METRIC_COLLECTED, self.on_metric_event)
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{len(self._patterns)} patterns · {self._app_map.rule_count} app-rules · "
                f"{self._signals_emitted} signals · {self._hebbian_wired} hebbian · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_signal_at,
        )

    # -------------------------------------------------------- BaselineLookup
    def zscore(self, collector: str, metric: str, value: float) -> float:
        baseline = self._baselines.get((collector, metric))
        return baseline.zscore(value) if baseline is not None else 0.0

    # --------------------------------------------------------- event handlers
    async def on_metric_event(self, event: Event) -> None:
        try:
            snapshot = event.payload.get("snapshot")
            if isinstance(snapshot, MetricSnapshot):
                await self._ingest(snapshot)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("diagnosis on_metric_event failed")
            self.event_bus.publish(
                system_error_event(module="diagnosis", exception=str(exc), severity="handler")
            )

    async def on_app_switch(self, event: Event) -> None:
        """`APP_SWITCH` -> a synthetic `"activity"` snapshot classified through
        the `AppMap` (and, for a browser tab, the `webapp` label + domain the
        collector already resolved), then the same ingest path as any collector
        reading (D-10, B14)."""
        try:
            app_id = event.payload.get("app_id")
            if not isinstance(app_id, str) or not app_id:
                return
            raw_webapp = event.payload.get("webapp")
            webapp = raw_webapp if isinstance(raw_webapp, str) and raw_webapp else None
            raw_wdom = event.payload.get("webapp_domain")
            webapp_domain = raw_wdom if isinstance(raw_wdom, str) and raw_wdom else None

            app_domain = self._app_map.classify(app_id) or ""
            excluded = self._focus_excluded(app_id)
            if app_domain:
                await self._classify_into_graph(app_id, app_domain)
            elif webapp is None and not excluded:
                # V-1 · an app the user really focuses is activity even when
                # `app_map` has no domain for it — before, it never got a node
                # here and so never took part in co-activation at all.
                # lock-free existence check first: a focus storm between two
                # unmapped apps must not take the graph lock on every event
                node_id, label = self._canon_app_id(app_id)
                if not self._graph.has_node(node_id):
                    await self._graph.upsert_node(node_id, NodeType.APP, {"label": label})
            if webapp is not None:
                await self._classify_webapp_into_graph(webapp, app_id, webapp_domain)

            # V-10 · a focus is the most direct sighting there is, yet only the
            # census (apps over `process_min_rss_mb`) ever wrote first/last-seen,
            # so a light app you live in all day had neither. A tab is a sighting
            # of its browser too. Rate-limited per node; excluded windows (dialogs,
            # portals) are not activity and are not stamped.
            if webapp is not None:
                await self._mark_sighting(f"webapp:{webapp}")
                await self._mark_sighting(self._canon_app_id(app_id)[0])
            elif not excluded:
                await self._mark_sighting(self._canon_app_id(app_id)[0])

            # T7 · Hebbian co-activation — the focused node is the tab if we
            # identified one, else the app. Both nodes now exist (upserted above).
            if webapp is not None:
                await self._reinforce_coactivation(f"webapp:{webapp}")
            elif not excluded:
                await self._reinforce_coactivation(self._canon_app_id(app_id)[0])

            # The focus's domain is the web-app's when we identified one, else the
            # browser's own (`brave -> habits`). This is what FocusSessionPattern
            # reads — so 20 min in GitHub tabs now counts as engineering focus.
            effective_domain = webapp_domain if webapp is not None else app_domain
            snapshot = MetricSnapshot(
                collector_name=_ACTIVITY_COLLECTOR,
                timestamp=event.timestamp,
                data={
                    "app_id": app_id,
                    "webapp": webapp,
                    "previous_app_id": event.payload.get("previous_app_id"),
                    "domain": effective_domain or "",
                },
            )
            await self._ingest(snapshot)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("diagnosis on_app_switch failed")
            self.event_bus.publish(
                system_error_event(module="diagnosis", exception=str(exc), severity="handler")
            )

    async def _ingest(self, snapshot: MetricSnapshot) -> None:
        name = snapshot.collector_name
        cap = self._max_samples(name)

        # (1) append to this collector's bounded window
        self._window_for(name, cap).append(snapshot)

        # (2) update the per-metric baselines
        for key, raw in snapshot.data.items():
            value = _numeric(raw)
            if value is not None:
                self._baselines.setdefault((name, key), MetricBaseline(cap)).observe(value)

        # (3) run every pattern that reads this collector — pure, synchronous
        for pattern in self._patterns:
            if name not in pattern.collectors:
                continue
            draft = pattern.evaluate(self._window_view(pattern), self)
            if draft is None:
                continue
            # (4) write the implicated nodes — one lock-cycle per mutation
            signal = await self._update_graph(draft)
            # (5-7) publish
            self._publish(pattern, signal)

    def _canon_app_id(self, raw: str) -> tuple[str, str]:
        """`(node_id, label)` for an app, canonicalised so one real app is one
        node no matter which sensor's name reached here (B17). Memoised per raw
        id — resolving is regex work, and a focus storm repeats a handful of ids
        thousands of times; the cache is bounded by distinct ids, reset if huge."""
        hit = self._canon_cache.get(raw)
        if hit is None:
            key = self._identity.resolve(raw) or raw
            hit = (f"app:{key}", self._identity.pretty(key) or key)
            if len(self._canon_cache) >= _CANON_CACHE_MAX:
                self._canon_cache.clear()
            self._canon_cache[raw] = hit
        return hit

    async def _classify_into_graph(self, app_id: str, domain_id: str) -> None:
        """Ensure `app:<canonical>` exists and is wired to its domain hub. Bounded
        by the number of distinct apps ever seen — `_known_apps` skips the repeat
        edge write on every subsequent switch to the same app (rules.md §3)."""
        node_id, label = self._canon_app_id(app_id)
        await self._graph.upsert_node(node_id, NodeType.APP, {"label": label})
        if node_id not in self._known_apps:
            await self._graph.add_edge(node_id, domain_id, RelationType.PART_OF)
            self._known_apps.add(node_id)

    async def _classify_webapp_into_graph(
        self, webapp: str, app_id: str, domain_id: str | None
    ) -> None:
        """Ensure `webapp:<label>` exists, wired `PART_OF` its browser and
        `PART_OF` its routing domain. `_known_webapps` writes both edges exactly
        once — re-adding an edge resets its Hebbian `weight` (rules.md §3)."""
        node_id = f"webapp:{webapp}"
        await self._graph.upsert_node(node_id, NodeType.WEBAPP, {"label": webapp})
        browser_id, browser_label = self._canon_app_id(app_id)
        if webapp not in self._known_webapps:
            # the browser node may not exist yet if the browser is unclassified
            await self._graph.upsert_node(browser_id, NodeType.APP, {"label": browser_label})
            await self._graph.add_edge(node_id, browser_id, RelationType.PART_OF)
            if domain_id:
                await self._graph.add_edge(node_id, domain_id, RelationType.PART_OF)
            self._known_webapps.add(webapp)

    async def _mark_sighting(self, node_id: str) -> None:
        """V-10 · stamp `node_id` as seen now, at most once per
        `_SIGHTING_REFRESH_SECONDS`. The gate is a lock-free dict read, so a
        repeat focus inside the window costs no graph lock at all; only a stale
        sighting pays one `mark_seen` lock cycle, which never touches the node's
        activity or score."""
        now = _now()
        if now - self._sighted_at.get(node_id, -math.inf) < _SIGHTING_REFRESH_SECONDS:
            return
        self._sighted_at[node_id] = now
        if len(self._sighted_at) > _CANON_CACHE_MAX:
            self._sighted_at = {
                nid: at
                for nid, at in self._sighted_at.items()
                if now - at < _SIGHTING_REFRESH_SECONDS
            }
        await self._graph.mark_seen(node_id, datetime.now(UTC))

    def _focus_excluded(self, app_id: str) -> bool:
        key = self._canon_app_id(app_id)[0].removeprefix("app:")
        return app_id.lower() in self._focus_exclude or key.lower() in self._focus_exclude

    async def _reinforce_coactivation(self, focus_id: str) -> None:
        """T7 · "fire together, wire together" — the state/credit math lives in
        `CoactivationWindow` (core/coactivation.py, S0); this just supplies the
        clock and does the actual graph mutation. Star-shaped
        (`wire_coactivation`): the recent apps are never re-wired among
        themselves."""
        now = _now()
        warm = self._window.warm_peers(focus_id, now)
        if warm:
            created, bumped = await self._graph.wire_coactivation(
                focus_id, warm, rate=self._hebbian_delta
            )
            self._hebbian_wired += created + bumped
            self._window.record_wiring(focus_id, warm, now)
        self._window.push_focus(focus_id, now)

    # --------------------------------------------------------------- helpers
    def _max_samples(self, collector: str) -> int:
        poll = float(self._poll_intervals.get(collector, 60.0))
        return max(2, math.ceil(self._window_seconds / max(1.0, poll)) + 1)

    def _window_for(self, collector: str, cap: int) -> deque[MetricSnapshot]:
        dq = self._windows.get(collector)
        if dq is None:
            dq = deque(maxlen=cap)
            self._windows[collector] = dq
        return dq

    def _window_view(self, pattern: BasePattern) -> dict[str, tuple[MetricSnapshot, ...]]:
        return {name: tuple(self._windows.get(name, ())) for name in pattern.collectors}

    def _canon_node_id(self, node_id: str) -> tuple[str, str | None]:
        """Canonicalise an `app:` node id from a pattern's `NodeSpec` (B17). The
        census keys these by process name; the focus sensor by Wayland app_id —
        one real app must be one node. Non-`app:` ids pass through unchanged."""
        if not node_id.startswith("app:"):
            return node_id, None
        canon, label = self._canon_app_id(node_id[4:])
        return canon, label

    async def _update_graph(self, draft: SignalDraft) -> Signal:
        related: list[str] = []
        canon_of: dict[str, str] = {}
        for spec in draft.node_specs:
            node_id, relabel = self._canon_node_id(spec.node_id)
            canon_of[spec.node_id] = node_id
            attrs: dict[str, object] = {
                "label": relabel or spec.label,
                **dict(spec.attributes),
            }
            await self._graph.upsert_node(node_id, spec.node_type, attrs)
            related.append(node_id)
        for spec in draft.node_specs:
            source_id = canon_of[spec.node_id]
            for target_id, relation in spec.edges:
                await self._graph.add_edge(source_id, self._canon_node_id(target_id)[0], relation)
        return Signal(
            signal_type=draft.signal_type,
            confidence=round(_clamp01(draft.confidence), 3),
            related_node_ids=tuple(related),
            source_snapshots=draft.source_snapshots,
            reason=draft.reason,
        )

    def _publish(self, pattern: BasePattern, signal: Signal) -> None:
        self.event_bus.publish(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": signal},
            )
        )
        if signal.related_node_ids:
            self.event_bus.publish(
                Event(
                    event_type=EventType.MEMORY_UPDATED,
                    source="diagnosis",
                    payload={
                        "node_ids": list(signal.related_node_ids),
                        "operation": "signal_correlate",
                    },
                )
            )
        self.event_bus.publish(
            Event(
                event_type=EventType.PATTERN_DETECTED,
                source="diagnosis",
                payload={"pattern": type(pattern).__name__, "confidence": signal.confidence},
            )
        )
        self._signals_emitted += 1
        self._last_signal_at = signal.timestamp


# gen-ref: 8ed6f90a
