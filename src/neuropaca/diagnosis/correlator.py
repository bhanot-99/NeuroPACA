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
from collections import deque
from collections.abc import Sequence
from datetime import datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.diagnosis.app_map import AppMap
from neuropaca.diagnosis.patterns import BasePattern, build_patterns
from neuropaca.diagnosis.signal import MetricBaseline, Signal, SignalDraft
from neuropaca.sensing.snapshot import MetricSnapshot

_log = logging.getLogger(__name__)

# APP_SWITCH events have no fixed cadence; this nominal interval only bounds the
# synthetic "activity" deque (maxlen = ceil(correlation_window / this) + 1, D-10).
_ACTIVITY_NOMINAL_POLL = 2.0
_ACTIVITY_COLLECTOR = "activity"


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
        self._known_apps: set[str] = set()
        self._windows: dict[str, deque[MetricSnapshot]] = {}
        self._baselines: dict[tuple[str, str], MetricBaseline] = {}
        self._signals_emitted = 0
        self._errors = 0
        self._last_signal_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self._app_map = AppMap.from_file(self._app_map_path)
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
                f"{self._signals_emitted} signals · {self._errors} errors"
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
        the `AppMap`, then the same ingest path as any collector reading (D-10)."""
        try:
            app_id = event.payload.get("app_id")
            if not isinstance(app_id, str) or not app_id:
                return
            domain = self._app_map.classify(app_id) or ""
            snapshot = MetricSnapshot(
                collector_name=_ACTIVITY_COLLECTOR,
                timestamp=event.timestamp,
                data={
                    "app_id": app_id,
                    "previous_app_id": event.payload.get("previous_app_id"),
                    "title": event.payload.get("title", ""),
                    "domain": domain,
                },
            )
            if domain:
                await self._classify_into_graph(app_id, domain)
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

    async def _classify_into_graph(self, app_id: str, domain_id: str) -> None:
        """Ensure `app:<id>` exists and is wired to its domain hub. Bounded by
        the number of distinct apps ever seen — `_known_apps` skips the repeat
        edge write on every subsequent switch to the same app (rules.md §3)."""
        node_id = f"app:{app_id}"
        await self._graph.upsert_node(node_id, NodeType.APP, {"label": app_id})
        if app_id not in self._known_apps:
            await self._graph.add_edge(node_id, domain_id, RelationType.PART_OF)
            self._known_apps.add(app_id)

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

    async def _update_graph(self, draft: SignalDraft) -> Signal:
        related: list[str] = []
        for spec in draft.node_specs:
            await self._graph.upsert_node(spec.node_id, spec.node_type, {"label": spec.label})
            related.append(spec.node_id)
        for spec in draft.node_specs:
            for target_id, relation in spec.edges:
                await self._graph.add_edge(spec.node_id, target_id, relation)
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
