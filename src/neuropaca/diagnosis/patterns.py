"""L3 · the pattern registry (Architecture.md §5, D-8).

B3 ships two patterns — `HighLoadPattern` and `IdlePattern`. Both are the same
shape: a per-snapshot predicate that must hold for N consecutive snapshots of the
primary collector, edge-triggered so a signal fires once per episode and re-arms
only when a reset predicate holds. `_RunLengthPattern` captures that idiom;
`FocusSessionPattern` / `DistractionPattern` (deferred to B2.5) will not share it.

Patterns are **pure and synchronous**. They read a window of snapshots plus a
read-only baseline and return a `SignalDraft` of *strings*. `SignalCorrelator`
does every graph mutation and every publish (D-8).
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import ClassVar, Protocol

from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType, SignalType
from neuropaca.diagnosis.signal import NodeSpec, SignalDraft
from neuropaca.sensing.collectors.system import ACTIVE_CPU_PERCENT, IDLE_CPU_PERCENT
from neuropaca.sensing.snapshot import MetricSnapshot

HIGH_LOAD_CPU_PERCENT = 90.0
_HIGH_LOAD_SUSTAIN_SECONDS = 300.0
_RELATED_FILE_CAP = 5


class BaselineLookup(Protocol):
    """What a pattern may ask of the correlator's baselines — read-only."""

    def zscore(self, collector: str, metric: str, value: float) -> float: ...


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _cpu(snapshot: MetricSnapshot) -> float | None:
    raw = snapshot.data.get("cpu_percent")
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


class BasePattern(ABC):
    """Pure, synchronous, edge-triggered. Holds no `EventBus` / `GraphMemory`
    and never `await`s. Adding one is a class here plus a line in
    `build_patterns()` — `SignalCorrelator` does not change (Architecture.md §5)."""

    signal_type: ClassVar[SignalType]
    collectors: ClassVar[tuple[str, ...]]

    @abstractmethod
    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        """Return a draft to emit a signal now, or `None`."""


class _RunLengthPattern(BasePattern):
    """Fire when `_hit()` holds for `min_run` consecutive snapshots of
    `collectors[0]`; re-arm only once `_cleared()` holds."""

    def __init__(self, min_run: int) -> None:
        self._min_run = max(1, min_run)
        self._firing = False

    @abstractmethod
    def _hit(self, snapshot: MetricSnapshot) -> bool: ...

    @abstractmethod
    def _cleared(self, snapshot: MetricSnapshot) -> bool: ...

    @abstractmethod
    def _draft(
        self,
        run: Sequence[MetricSnapshot],
        windows: Mapping[str, Sequence[MetricSnapshot]],
        baselines: BaselineLookup,
    ) -> SignalDraft: ...

    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        window = windows.get(self.collectors[0], ())
        if not window:
            return None
        if self._firing:
            if self._cleared(window[-1]):
                self._firing = False
            return None
        run = self._trailing_run(window)
        if len(run) >= self._min_run:
            self._firing = True
            return self._draft(run, windows, baselines)
        return None

    def _trailing_run(self, window: Sequence[MetricSnapshot]) -> list[MetricSnapshot]:
        run: list[MetricSnapshot] = []
        for snapshot in reversed(window):
            if not self._hit(snapshot):
                break
            run.append(snapshot)
        run.reverse()
        return run


class HighLoadPattern(_RunLengthPattern):
    """`system.cpu_percent > 90` sustained ≥ 5 min (blueprint F2). Related nodes
    are the files changed during the load window — strictly from `filesystem`
    activity (D-8); no process names exist in B3."""

    signal_type: ClassVar[SignalType] = SignalType.HIGH_LOAD
    collectors: ClassVar[tuple[str, ...]] = ("system", "filesystem")

    def __init__(
        self, *, poll_seconds: float, cpu_threshold: float = HIGH_LOAD_CPU_PERCENT
    ) -> None:
        super().__init__(min_run=math.ceil(_HIGH_LOAD_SUSTAIN_SECONDS / max(1.0, poll_seconds)))
        self._poll_seconds = poll_seconds
        self._threshold = cpu_threshold

    def _hit(self, snapshot: MetricSnapshot) -> bool:
        cpu = _cpu(snapshot)
        return cpu is not None and cpu > self._threshold

    def _cleared(self, snapshot: MetricSnapshot) -> bool:
        cpu = _cpu(snapshot)
        return cpu is not None and cpu <= self._threshold

    def _draft(
        self,
        run: Sequence[MetricSnapshot],
        windows: Mapping[str, Sequence[MetricSnapshot]],
        baselines: BaselineLookup,
    ) -> SignalDraft:
        cpus = [c for c in (_cpu(s) for s in run) if c is not None]
        mean_cpu = sum(cpus) / len(cpus) if cpus else self._threshold
        margin = _clamp01(0.5 + (mean_cpu - self._threshold) / (100.0 - self._threshold) * 0.5)
        z = baselines.zscore("system", "cpu_percent", mean_cpu)
        confidence = _clamp01(margin + 0.05 * max(0.0, z - 1.0))
        specs = self._file_specs(run[0].timestamp, windows.get("filesystem", ()))
        minutes = len(run) * self._poll_seconds / 60.0
        reason = (
            f"cpu {mean_cpu:.0f}% (> {self._threshold:.0f}%) for {len(run)} samples "
            f"(~{minutes:.0f} min)"
        )
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=tuple(run),
            node_specs=specs,
            reason=reason,
        )

    @staticmethod
    def _file_specs(since: datetime, fs_window: Sequence[MetricSnapshot]) -> tuple[NodeSpec, ...]:
        counts: Counter[str] = Counter()
        for snapshot in fs_window:
            if snapshot.timestamp < since:
                continue
            paths = snapshot.data.get("changed_paths")
            if isinstance(paths, (list, tuple)):
                counts.update(str(p) for p in paths)
        specs: list[NodeSpec] = []
        for path, _count in counts.most_common(_RELATED_FILE_CAP):
            label = os.path.basename(path.rstrip("/")) or path
            specs.append(NodeSpec(node_id=f"file:{path}", node_type=NodeType.FILE, label=label))
        return tuple(specs)


class IdlePattern(_RunLengthPattern):
    """`system.cpu_percent < 5` for ≥ `idle_threshold_seconds` (blueprint F2).
    Shares `IDLE_CPU_PERCENT` / `ACTIVE_CPU_PERCENT` with L2's `_IdleWatcher` so
    the L3 `IDLE` signal and the L2 `IDLE_DETECTED` edge never disagree. Distinct
    consumers: this signal → L4/L5; the L2 edge → L6."""

    signal_type: ClassVar[SignalType] = SignalType.IDLE
    collectors: ClassVar[tuple[str, ...]] = ("system",)

    def __init__(self, *, idle_threshold_seconds: int, poll_seconds: float) -> None:
        super().__init__(min_run=math.ceil(idle_threshold_seconds / max(1.0, poll_seconds)))

    def _hit(self, snapshot: MetricSnapshot) -> bool:
        cpu = _cpu(snapshot)
        return cpu is not None and cpu < IDLE_CPU_PERCENT

    def _cleared(self, snapshot: MetricSnapshot) -> bool:
        cpu = _cpu(snapshot)
        return cpu is not None and cpu >= ACTIVE_CPU_PERCENT

    def _draft(
        self,
        run: Sequence[MetricSnapshot],
        windows: Mapping[str, Sequence[MetricSnapshot]],
        baselines: BaselineLookup,
    ) -> SignalDraft:
        cpus = [c for c in (_cpu(s) for s in run) if c is not None]
        mean_cpu = sum(cpus) / len(cpus) if cpus else 0.0
        depth = _clamp01((IDLE_CPU_PERCENT - mean_cpu) / IDLE_CPU_PERCENT)
        confidence = _clamp01(0.7 + 0.3 * depth)
        idle_seconds = (run[-1].timestamp - run[0].timestamp).total_seconds()
        reason = f"cpu {mean_cpu:.1f}% (< {IDLE_CPU_PERCENT:.0f}%) for ~{idle_seconds / 60:.0f} min"
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=tuple(run),
            reason=reason,
        )


def build_patterns(config: Config) -> list[BasePattern]:
    """The B3 registry, in evaluation order. A 5th pattern is one more line."""
    system_poll = float(config.poll_intervals.get("system", 60.0))
    return [
        HighLoadPattern(poll_seconds=system_poll),
        IdlePattern(idle_threshold_seconds=config.idle_threshold_seconds, poll_seconds=system_poll),
    ]
