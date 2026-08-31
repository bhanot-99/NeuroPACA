"""L3 · the pattern registry (Architecture.md §5, D-8 / D-10).

B3 shipped two run-length patterns — `HighLoadPattern` and `IdlePattern`: a
per-snapshot predicate that must hold for N consecutive snapshots of the primary
collector, edge-triggered so a signal fires once per episode and re-arms only
when a reset predicate holds. `_RunLengthPattern` captures that idiom.

B2.5b adds two window-shaped patterns that read the `"activity"` pseudo-collector
(synthetic `MetricSnapshot`s the correlator makes from `APP_SWITCH` events, D-10):
`FocusSessionPattern` (a sustained deep-work episode) and `DistractionPattern`
(rapid context-switching). They do not share `_RunLengthPattern` — they reason
over an elapsed span, not a consecutive run.

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
from datetime import datetime, timedelta
from typing import ClassVar, Protocol

from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType, RelationType, SignalType
from neuropaca.diagnosis.signal import NodeSpec, SignalDraft
from neuropaca.sensing.collectors.system import ACTIVE_CPU_PERCENT, IDLE_CPU_PERCENT
from neuropaca.sensing.snapshot import MetricSnapshot

HIGH_LOAD_CPU_PERCENT = 90.0
_HIGH_LOAD_SUSTAIN_SECONDS = 300.0
_RELATED_FILE_CAP = 5

# --- B2.5b activity patterns (D-10) --------------------------------------------
_FOCUS_MIN_SECONDS = 1200.0  # 20 min, blueprint F2
_FOCUS_DOMAINS: frozenset[str] = frozenset({"domain:engineering", "domain:research"})
_DISTRACTION_WINDOW_SECONDS = 120.0  # 2 min, blueprint F2
_DISTRACTION_MAX_SWITCHES = 5  # "> 5x" -> fires at the 6th
_DISTRACTION_REARM_SWITCHES = 2  # rate has settled -> re-arm


class BaselineLookup(Protocol):
    """What a pattern may ask of the correlator's baselines — read-only."""

    def zscore(self, collector: str, metric: str, value: float) -> float: ...


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _cpu(snapshot: MetricSnapshot) -> float | None:
    raw = snapshot.data.get("cpu_percent")
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _str_field(snapshot: MetricSnapshot, key: str) -> str | None:
    raw = snapshot.data.get(key)
    return raw if isinstance(raw, str) and raw else None


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


class FocusSessionPattern(BasePattern):
    """A sustained deep-work episode (blueprint F2, D-10).

    Fires once when the focused app has classified into `domain:engineering` or
    `domain:research` for >= 20 min with no switch away, and the machine was not
    idle across that span. The blueprint's "high CPU" is read as "not idle" —
    editor-driven focus work rarely pins a core, so the gate is mean
    `system.cpu_percent` >= `ACTIVE_CPU_PERCENT` over the span, not the HIGH_LOAD
    threshold. Re-arms when the focus domain is left.

    The last `"activity"` snapshot is the current focus; a switch appends a new
    one, so "no switch away" == "the last activity snapshot is still in-focus".
    """

    signal_type: ClassVar[SignalType] = SignalType.FOCUS_SESSION
    collectors: ClassVar[tuple[str, ...]] = ("system", "activity")

    def __init__(self, *, min_seconds: float = _FOCUS_MIN_SECONDS) -> None:
        self._min_seconds = min_seconds
        self._firing = False

    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        activity = windows.get("activity", ())
        if not activity:
            return None
        current = activity[-1]
        domain = _str_field(current, "domain")
        in_focus = domain in _FOCUS_DOMAINS

        if self._firing:
            if not in_focus:
                self._firing = False
            return None
        if not in_focus or domain is None:
            return None

        system = windows.get("system", ())
        latest_system = system[-1].timestamp if system else current.timestamp
        now = max(current.timestamp, latest_system)
        held = (now - current.timestamp).total_seconds()
        if held < self._min_seconds:
            return None

        span = [s for s in system if s.timestamp >= current.timestamp]
        cpus = [c for c in (_cpu(s) for s in span) if c is not None]
        if not cpus:
            return None
        mean_cpu = sum(cpus) / len(cpus)
        if mean_cpu < ACTIVE_CPU_PERCENT:
            return None

        self._firing = True
        app_id = _str_field(current, "app_id") or "unknown"
        slug = domain.split(":", 1)[1]
        spec = NodeSpec(
            node_id=f"app:{app_id}",
            node_type=NodeType.APP,
            label=app_id,
            edges=((domain, RelationType.PART_OF),),
        )
        over = _clamp01((held - self._min_seconds) / self._min_seconds)
        confidence = _clamp01(0.6 + 0.4 * over)
        reason = (
            f"{app_id} ({slug}) focused for ~{held / 60:.0f} min, cpu ~{mean_cpu:.0f}% (active)"
        )
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=(current, *span),
            node_specs=(spec,),
            reason=reason,
        )


class DistractionPattern(BasePattern):
    """Rapid context-switching: more than 5 `APP_SWITCH` events inside a trailing
    2-minute window (blueprint F2, D-10). Re-arms once the switch rate settles
    back to <= 2 in the window. Writes no nodes — like `IdlePattern`, the signal
    itself is the payload L4/L5 consume."""

    signal_type: ClassVar[SignalType] = SignalType.DISTRACTION
    collectors: ClassVar[tuple[str, ...]] = ("activity",)

    def __init__(
        self,
        *,
        window_seconds: float = _DISTRACTION_WINDOW_SECONDS,
        max_switches: int = _DISTRACTION_MAX_SWITCHES,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_switches = max_switches
        self._firing = False

    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        activity = windows.get("activity", ())
        if not activity:
            return None
        cutoff = activity[-1].timestamp - timedelta(seconds=self._window_seconds)
        recent = [s for s in activity if s.timestamp >= cutoff]
        count = len(recent)

        if self._firing:
            if count <= _DISTRACTION_REARM_SWITCHES:
                self._firing = False
            return None
        if count <= self._max_switches:
            return None

        self._firing = True
        distinct: list[str] = []
        for snapshot in recent:
            app_id = _str_field(snapshot, "app_id")
            if app_id and app_id not in distinct:
                distinct.append(app_id)
        confidence = _clamp01(0.5 + 0.1 * (count - self._max_switches))
        reason = (
            f"{count} app switches in {self._window_seconds / 60:.0f} min "
            f"({len(distinct)} distinct)"
        )
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=tuple(recent),
            reason=reason,
        )


def build_patterns(config: Config) -> list[BasePattern]:
    """The pattern registry, in evaluation order. Adding one is a class above
    plus a line here — `SignalCorrelator` does not change (Architecture.md §5)."""
    system_poll = float(config.poll_intervals.get("system", 60.0))
    return [
        HighLoadPattern(poll_seconds=system_poll),
        IdlePattern(idle_threshold_seconds=config.idle_threshold_seconds, poll_seconds=system_poll),
        FocusSessionPattern(),
        DistractionPattern(),
    ]
