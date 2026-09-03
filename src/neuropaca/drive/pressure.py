"""L5 · `PressureAccumulator` — action gradients (Architecture.md §7, B7, D-14).

Nothing in NeuroPACA acts because *one* thing happened. Evidence accumulates per
node and decays; only a node whose pressure is still high when the next piece of
evidence lands can reach a threshold.

Two independent sources feed it, and it subscribes to **exactly** those two
(D-14 — the blueprint's L2 arrow was retired because `MetricSnapshot.anomaly_score`
is fixed at 0.0 by D-7 B5, so L2 has no spike magnitude to contribute):

- `SIGNAL_CORRELATED` from L3 Diagnosis — a correlated behavioural signal;
- `INSIGHT_GENERATED` from L4 Learning — an extractive insight over such a signal.

They never call each other and never call this module (rules.md §0.4).

**The gradient** (Architecture.md §7):

- **low** — `pressure >= pressure_low_threshold`. One Diagnosis spike is enough.
  A *safe* action fires silently.
- **high** — `pressure >= pressure_high_threshold` **and** L3 and L4 have each
  contributed a high-confidence spike inside the corroboration window. A
  *dangerous* action becomes permissible — permissible, not automatic: the L7
  gate still requires a recorded human confirmation (rules.md §5.2).

The high tier is structurally unreachable from a single source: the corroboration
test is a set test over `{"diagnosis", "learning"}`, so no magnitude of one
source's evidence can satisfy it. That is the B7 exit criterion "a single signal
never crosses the high threshold" — an invariant of the shape, not a tuned number.

**Decay** is exponential with a half-life (`pressure_decay_half_life_seconds`,
60 s => 50 %/min). It is applied two ways, and they agree exactly:

- lazily, on every read and every `add_pressure`, from the entry's own last-update
  timestamp — so the value is correct at any instant;
- on a timer (`pressure_decay_interval_seconds`) driven by the injected `Clock`,
  which is what evicts entries that have decayed into irrelevance and releases
  their threshold latch.

After 10 minutes of silence a spike retains `0.5 ** 10 = 0.098 %` — inside the
"< 1 % within 10 min" exit bound with an order of magnitude to spare.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.insight import Insight

_log = logging.getLogger(__name__)

# The two source names that may contribute. Both are required, inside the
# corroboration window, before the high tier is even considered.
SOURCE_DIAGNOSIS = "diagnosis"
SOURCE_LEARNING = "learning"
_REQUIRED_HIGH_SOURCES = frozenset({SOURCE_DIAGNOSIS, SOURCE_LEARNING})

# A contribution only counts toward corroboration above this confidence. Same
# bar L9 uses to surface an insight (B5) — "worth telling you about" and "worth
# letting the system act on" are the same threshold.
_CORROBORATION_MIN_CONFIDENCE = 0.75
# "Simultaneously" (Architecture.md §7) expressed against the decay constant:
# two half-lives, after which a contribution has already fallen to 25 %.
_CORROBORATION_WINDOW_HALF_LIVES = 2.0

# Below this an entry is noise; it is evicted and its latch released. 0.001 is
# a tenth of the "< 1 %" exit bound at the default low threshold of 1.0.
_EVICT_BELOW = 0.001
# Hysteresis: a latched tier re-arms only once pressure falls back under this
# fraction of its threshold, so one node cannot machine-gun the bus while it
# hovers on the line.
_LATCH_RELEASE_FRACTION = 0.9

# Per-contribution weights. An insight is corroboration *about* a signal, not a
# second independent observation of it, so it is worth slightly less on its own;
# what makes it valuable is that it comes from a different layer.
_SIGNAL_WEIGHT = 1.0
_INSIGHT_WEIGHT = 0.8


@dataclass(frozen=True, slots=True)
class PressureEntry:
    """An immutable snapshot of one node's accumulated pressure
    (Architecture.md §7).

    `reason` is the blueprint's requirement made structural: **an action that
    cannot explain itself must not fire**, so every entry carries the human
    sentence that justifies it and `PRESSURE_THRESHOLD_REACHED` cannot be
    published without one.

    `sources` is additive to the blueprint (D-14) and load-bearing: it is what
    makes the high tier's corroboration test possible.
    """

    node_id: str
    pressure: float
    reason: str
    created_at: datetime
    last_updated: datetime
    sources: tuple[str, ...] = ()


@dataclass(slots=True)
class _Accumulation:
    """The mutable interior. Never handed out — `PressureEntry` is the view."""

    node_id: str
    pressure: float
    reason: str
    created_at: datetime
    last_updated: datetime
    updated_monotonic: float
    # source -> monotonic time of that source's last high-confidence contribution
    corroboration: dict[str, float] = field(default_factory=dict)
    latched: str = ""  # "", "low", or "high"

    def view(self) -> PressureEntry:
        return PressureEntry(
            node_id=self.node_id,
            pressure=self.pressure,
            reason=self.reason,
            created_at=self.created_at,
            last_updated=self.last_updated,
            sources=tuple(sorted(self.corroboration)),
        )


class PressureAccumulator(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("drive", event_bus, config)
        self._graph = graph_memory
        self._clock: Clock = clock or SystemClock()
        self._entries: dict[str, _Accumulation] = {}
        self._decay_task: asyncio.Task[None] | None = None
        self._contributions = 0
        self._low_crossings = 0
        self._high_crossings = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.SIGNAL_CORRELATED, self.on_signal_event)
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_event)

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._decay_task = asyncio.create_task(self._decay_loop())

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.SIGNAL_CORRELATED, self.on_signal_event)
        self.event_bus.unsubscribe(EventType.INSIGHT_GENERATED, self.on_insight_event)
        task, self._decay_task = self._decay_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._entries.clear()

    def health(self) -> ModuleHealth:
        top = ""
        if self._entries:
            hottest = max(self._entries.values(), key=lambda a: a.pressure)
            top = f" · top {hottest.node_id}@{hottest.pressure:.2f}"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{len(self._entries)} tracked · {self._contributions} contributions · "
                f"{self._low_crossings} low · {self._high_crossings} high · "
                f"{self._errors} errors{top}"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_signal_event(self, event: Event) -> None:
        """L3 spike. Pressure lands on the signal's related nodes."""
        try:
            signal = event.payload.get("signal")
            if not isinstance(signal, Signal):
                return
            reason = signal.reason or f"{signal.signal_type} signal"
            for node_id in signal.related_node_ids:
                self.add_pressure(
                    node_id,
                    _SIGNAL_WEIGHT * signal.confidence,
                    f"L3 {signal.signal_type}: {reason}",
                    source=SOURCE_DIAGNOSIS,
                    confidence=signal.confidence,
                )
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._fail("on_signal_event", exc)

    async def on_insight_event(self, event: Event) -> None:
        """L4 (and L6) corroboration. Pressure lands on the cited nodes.

        Only an `INSIGHT` written by Learning corroborates — an L6 idle thought
        is the system talking to itself during idle, not independent evidence,
        so it contributes nothing. Filtering on the *event source* keeps that
        judgement in one place.
        """
        try:
            insight = event.payload.get("insight")
            if not isinstance(insight, Insight) or event.source != SOURCE_LEARNING:
                return
            for node_id in insight.cited_node_ids:
                self.add_pressure(
                    node_id,
                    _INSIGHT_WEIGHT * insight.confidence,
                    f"L4 {insight.category}: {insight.summary}",
                    source=SOURCE_LEARNING,
                    confidence=insight.confidence,
                )
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._fail("on_insight_event", exc)

    def _fail(self, where: str, exc: Exception) -> None:
        self._errors += 1
        _log.exception("drive %s failed", where)
        self.event_bus.publish(
            system_error_event(module="drive", exception=str(exc), severity="handler")
        )

    # ------------------------------------------------------------ accumulation
    def add_pressure(
        self,
        node_id: str,
        amount: float,
        reason: str,
        *,
        source: str = SOURCE_DIAGNOSIS,
        confidence: float = 1.0,
    ) -> PressureEntry:
        """Decay this node to *now*, add `amount`, then re-evaluate the tiers.

        Synchronous by design: it mutates only this module's own dict — no lock,
        no graph I/O, no await — so an event handler cannot stall the bus.
        """
        if amount <= 0 or not reason:
            # No reason, no pressure (Architecture.md §7). Silently ignoring a
            # reasonless contribution is safer than accumulating an unexplainable one.
            return self._snapshot(node_id)

        now_mono = self._clock.monotonic()
        now_wall = self._clock.now()
        entry = self._entries.get(node_id)
        if entry is None:
            entry = _Accumulation(
                node_id=node_id,
                pressure=0.0,
                reason=reason,
                created_at=now_wall,
                last_updated=now_wall,
                updated_monotonic=now_mono,
            )
            self._entries[node_id] = entry
        else:
            self._decay_entry(entry, now_mono)

        entry.pressure += amount
        entry.reason = reason  # the most recent justification is the live one
        entry.last_updated = now_wall
        entry.updated_monotonic = now_mono
        if confidence >= _CORROBORATION_MIN_CONFIDENCE:
            entry.corroboration[source] = now_mono

        self._contributions += 1
        self._last_at = now_wall
        self._publish_if_over_threshold(entry, now_mono)
        return entry.view()

    def decay(self) -> None:
        """Apply the elapsed decay to every entry and evict what has faded.

        Idempotent and safe to call at any cadence — decay is computed from each
        entry's own timestamp, so calling it twice in a row changes nothing the
        second time.
        """
        now_mono = self._clock.monotonic()
        for node_id in list(self._entries):
            entry = self._entries[node_id]
            self._decay_entry(entry, now_mono)
            if entry.pressure < _EVICT_BELOW:
                del self._entries[node_id]
            elif entry.latched and entry.pressure < self._release_level(entry.latched):
                entry.latched = ""

    def _decay_entry(self, entry: _Accumulation, now_mono: float) -> None:
        elapsed = now_mono - entry.updated_monotonic
        if elapsed <= 0:
            return
        half_life = float(self.config.pressure_decay_half_life_seconds)
        entry.pressure *= 0.5 ** (elapsed / half_life)
        entry.updated_monotonic = now_mono

    def _release_level(self, tier: str) -> float:
        threshold = (
            self.config.pressure_high_threshold
            if tier == "high"
            else self.config.pressure_low_threshold
        )
        return threshold * _LATCH_RELEASE_FRACTION

    # ------------------------------------------------------------- thresholds
    def _publish_if_over_threshold(self, entry: _Accumulation, now_mono: float) -> None:
        """Publish at most one `PRESSURE_THRESHOLD_REACHED` per crossing.

        `high` is checked first and supersedes a standing `low` latch: a node
        that has already fired the safe tier can still escalate once L3 and L4
        corroborate.
        """
        if entry.pressure >= self.config.pressure_high_threshold and self._is_corroborated(
            entry, now_mono
        ):
            if entry.latched != "high":
                entry.latched = "high"
                self._high_crossings += 1
                self._publish(entry, "high")
            return
        if entry.pressure >= self.config.pressure_low_threshold and not entry.latched:
            entry.latched = "low"
            self._low_crossings += 1
            self._publish(entry, "low")

    def _is_corroborated(self, entry: _Accumulation, now_mono: float) -> bool:
        """Both layers, both high-confidence, both inside the window."""
        window = _CORROBORATION_WINDOW_HALF_LIVES * self.config.pressure_decay_half_life_seconds
        fresh = {src for src, at in entry.corroboration.items() if now_mono - at <= window}
        return _REQUIRED_HIGH_SOURCES <= fresh

    def _publish(self, entry: _Accumulation, tier: str) -> None:
        view = entry.view()
        _log.info(
            "L5 pressure %s threshold on %s (%.2f) — %s", tier, view.node_id, view.pressure, tier
        )
        self.event_bus.publish(
            Event(
                event_type=EventType.PRESSURE_THRESHOLD_REACHED,
                source="drive",
                priority=5 if tier == "high" else 0,
                payload={"entry": view, "tier": tier},
            )
        )

    # ------------------------------------------------------------------ reads
    def get_top_pressures(self, n: int = 5) -> list[PressureEntry]:
        """The n hottest nodes, decayed to now. A read never mutates the tiers."""
        now_mono = self._clock.monotonic()
        for entry in self._entries.values():
            self._decay_entry(entry, now_mono)
        ranked = sorted(self._entries.values(), key=lambda a: (-a.pressure, a.node_id))
        return [entry.view() for entry in ranked[:n]]

    def current_pressure(self, node_id: str) -> float:
        entry = self._entries.get(node_id)
        if entry is None:
            return 0.0
        self._decay_entry(entry, self._clock.monotonic())
        return entry.pressure

    def _snapshot(self, node_id: str) -> PressureEntry:
        entry = self._entries.get(node_id)
        if entry is not None:
            return entry.view()
        now = self._clock.now()
        return PressureEntry(
            node_id=node_id, pressure=0.0, reason="", created_at=now, last_updated=now
        )

    @property
    def pressure_map(self) -> dict[str, float]:
        """The blueprint's `pressure_map: Dict[str, float]` — a decayed read-only
        projection. The interior carries reason, sources, and timestamps too."""
        now_mono = self._clock.monotonic()
        for entry in self._entries.values():
            self._decay_entry(entry, now_mono)
        return {node_id: entry.pressure for node_id, entry in self._entries.items()}

    # ------------------------------------------------------------- decay timer
    async def _decay_loop(self) -> None:
        interval = float(self.config.pressure_decay_interval_seconds)
        try:
            while True:
                await self._clock.sleep(interval)
                try:
                    self.decay()
                except Exception as exc:  # a timer tick never kills the daemon
                    self._fail("decay tick", exc)
        except asyncio.CancelledError:
            raise
