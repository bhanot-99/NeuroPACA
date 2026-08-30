"""L3 · Diagnosis data model (Architecture.md §5, D-8).

A pattern is pure and synchronous: it inspects a window of `MetricSnapshot`s plus
a read-only `MetricBaseline` and returns a `SignalDraft` — which carries **node
*specs* (id + type + label strings), never graph `Node`s**. `SignalCorrelator`
turns a draft into a `Signal`: it upserts the specs into `GraphMemory`, fills
`related_node_ids`, then publishes.

`MetricBaseline` is confidence-scaling only (D-8): a rolling mean / population
stddev over a bounded window. It never decides whether a pattern fires — the
blueprint's absolute thresholds do that, so a cold daemon still detects a real
spike at minute two.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from neuropaca.core.enums import NodeType, RelationType, SignalType
from neuropaca.sensing.snapshot import MetricSnapshot


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """A node a pattern wants ensured in the graph, plus any edges *out* of it.
    Every edge target must be another spec in the same draft or a routing hub —
    `add_edge` on a missing node creates an attribute-less phantom (D-8)."""

    node_id: str
    node_type: NodeType
    label: str
    edges: tuple[tuple[str, RelationType], ...] = ()


@dataclass(frozen=True, slots=True)
class SignalDraft:
    """A pattern's output before any graph I/O. Immutable; strings only."""

    signal_type: SignalType
    confidence: float
    source_snapshots: tuple[MetricSnapshot, ...]
    node_specs: tuple[NodeSpec, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Signal:
    """The correlated behavioural signal published on `SIGNAL_CORRELATED`
    (Architecture.md §5). Consumed by L4 and L5 independently."""

    signal_type: SignalType
    confidence: float
    related_node_ids: tuple[str, ...]
    source_snapshots: tuple[MetricSnapshot, ...]
    reason: str = ""
    timestamp: datetime = field(default_factory=_utcnow)


class MetricBaseline:
    """Rolling mean + population standard deviation over a bounded window.

    Confidence-scaling only (D-8). `zscore()` returns 0.0 until there are at
    least two samples and a non-zero spread, so an unwarmed baseline never
    perturbs a confidence score.
    """

    __slots__ = ("_values",)

    def __init__(self, window: int) -> None:
        self._values: deque[float] = deque(maxlen=max(2, window))

    def observe(self, value: float) -> None:
        self._values.append(float(value))

    @property
    def count(self) -> int:
        return len(self._values)

    def mean(self) -> float:
        return sum(self._values) / len(self._values) if self._values else 0.0

    def stddev(self) -> float:
        n = len(self._values)
        if n < 2:
            return 0.0
        mu = self.mean()
        return math.sqrt(sum((v - mu) ** 2 for v in self._values) / n)

    def zscore(self, value: float) -> float:
        sd = self.stddev()
        if sd == 0.0:
            return 0.0
        return (value - self.mean()) / sd
