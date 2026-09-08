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
from typing import Any, ClassVar, Protocol

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

# --- B13 resource-aware patterns (D-19) ---------------------------------------
_MEM_PRESSURE_Z = 2.0
_MEM_PRESSURE_FLOOR_MB = 1024.0
_MEM_PRESSURE_SUSTAIN_SECONDS = 180.0
_MEM_PRESSURE_CLEAR_Z = 1.0
_HEAVY_APP_CAP = 5  # most rows a single memory/working-set signal attaches
_FOCUS_RAM_CORROBORATION = 0.15  # confidence bump when the census backs the focus
_BROWSER_HINTS: frozenset[str] = frozenset(
    {"brave", "chrome", "chromium", "firefox", "safari", "edge", "webkit", "epiphany"}
)


def _num(snapshot: MetricSnapshot, key: str) -> float | None:
    raw = snapshot.data.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _rfloat(row: Mapping[str, Any], key: str) -> float:
    raw = row.get(key, 0.0)
    try:
        return float(raw) if raw is not None and not isinstance(raw, bool) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _census_rows(process_window: Sequence[MetricSnapshot]) -> list[dict[str, Any]]:
    """The most recent `process` census as a list of row dicts (RAM-sorted by the
    collector). Empty when the census has not run — every B13 resource pattern
    then attaches nothing and is effectively inert (D-19: that ordering is fine)."""
    for snapshot in reversed(process_window):
        rows = snapshot.data.get("processes")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict) and r.get("name")]
    return []


def _is_browser(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _BROWSER_HINTS)


def _heavy_app_specs(
    rows: Sequence[Mapping[str, Any]], now: datetime, *, cap: int = _HEAVY_APP_CAP
) -> tuple[NodeSpec, ...]:
    """`NodeSpec`s for the top `cap` censused apps, each carrying its durable
    resource attributes (schema v3). `first_seen_at` is derived from the row's
    `running_seconds` so the graph records a real process-start time; the upsert
    keeps it write-once. Node id is `app:<process name>` — where the process name
    and the Wayland app_id agree (most dev tools) this is the *same* node a focus
    event forms; where they differ (browsers: `brave` vs `brave-browser`) a
    round-2 name map merges them (B13 §7)."""
    specs: list[NodeSpec] = []
    for row in list(rows)[:cap]:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        first_seen = now - timedelta(seconds=max(0.0, _rfloat(row, "running_seconds")))
        specs.append(
            NodeSpec(
                node_id=f"app:{name}",
                node_type=NodeType.APP,
                label=name,
                attributes=(
                    ("ram_mb", _rfloat(row, "rss_mb")),
                    ("cpu_percent", _rfloat(row, "cpu_percent")),
                    ("first_seen_at", first_seen.isoformat()),
                    ("last_seen_at", now.isoformat()),
                ),
            )
        )
    return tuple(specs)


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
    consumers: this signal → L4/L5; the L2 edge → L6.

    B13-A (D-19(d)): the signal attaches to the **last-focused `app:<id>`** — "you
    went idle after working in X" — a session-boundary marker on connected,
    traversable structure. Not `YOU` (floods the one hub `find_related` refuses
    to traverse) and not a `SESSION` node (needs a correlator-side accumulator —
    its own phase). No activity data → nodeless, acceptable on a headless box."""

    signal_type: ClassVar[SignalType] = SignalType.IDLE
    collectors: ClassVar[tuple[str, ...]] = ("system", "activity")

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
        specs = self._last_app_spec(windows.get("activity", ()))
        if specs:
            reason += f" — last active in {specs[0].label}"
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=tuple(run),
            node_specs=specs,
            reason=reason,
        )

    @staticmethod
    def _last_app_spec(activity: Sequence[MetricSnapshot]) -> tuple[NodeSpec, ...]:
        for snapshot in reversed(activity):
            app_id = _str_field(snapshot, "app_id")
            if not app_id:
                continue
            webapp = _str_field(snapshot, "webapp")
            if webapp:
                return (
                    NodeSpec(node_id=f"webapp:{webapp}", node_type=NodeType.WEBAPP, label=webapp),
                )
            return (NodeSpec(node_id=f"app:{app_id}", node_type=NodeType.APP, label=app_id),)
        return ()


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

    B13-B4: RAM corroboration. A 20-min session emits `confidence = 0.6` — below
    L4's 0.7 gate, a named reason for zero insights. If the concurrent `process`
    census shows the focused app as the largest non-browser RSS group, add
    `_FOCUS_RAM_CORROBORATION` (+0.15) so a genuine deep-work session crosses the
    line. No new nodes, no new signal type — the bonus is confidence only.
    """

    signal_type: ClassVar[SignalType] = SignalType.FOCUS_SESSION
    collectors: ClassVar[tuple[str, ...]] = ("system", "activity", "process")

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
        webapp = _str_field(current, "webapp")
        slug = domain.split(":", 1)[1]
        if webapp:
            # attribute the session to the tab, not the whole browser. No edge —
            # `webapp:X --part_of--> domain` is owned by the APP_SWITCH
            # classification path (re-emitting it would reset the Hebbian weight,
            # same reasoning as DistractionPattern).
            focus_label = webapp
            spec = NodeSpec(node_id=f"webapp:{webapp}", node_type=NodeType.WEBAPP, label=webapp)
        else:
            focus_label = app_id
            spec = NodeSpec(
                node_id=f"app:{app_id}",
                node_type=NodeType.APP,
                label=app_id,
                edges=((domain, RelationType.PART_OF),),
            )
        over = _clamp01((held - self._min_seconds) / self._min_seconds)
        confidence = 0.6 + 0.4 * over
        reason = (
            f"{focus_label} ({slug}) focused for ~{held / 60:.0f} min, "
            f"cpu ~{mean_cpu:.0f}% (active)"
        )
        if self._ram_corroborates(app_id, windows.get("process", ())):
            confidence += _FOCUS_RAM_CORROBORATION
            reason += " — top RSS group (RAM-corroborated)"
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=_clamp01(confidence),
            source_snapshots=(current, *span),
            node_specs=(spec,),
            reason=reason,
        )

    @staticmethod
    def _ram_corroborates(app_id: str, process_window: Sequence[MetricSnapshot]) -> bool:
        """True when the focused app is the largest **non-browser** RSS group in
        the census — a fuzzy name match (`zed` <-> `dev.zed.Zed`) since the
        Wayland app_id and the process name rarely match exactly."""
        rows = [r for r in _census_rows(process_window) if not _is_browser(str(r.get("name", "")))]
        if not rows:
            return False
        top = max(rows, key=lambda r: _rfloat(r, "rss_mb"))
        name = str(top.get("name", "")).lower()
        aid = app_id.lower()
        if not name:
            return False
        return name in aid or aid.split(".")[-1] in name or name.split("-")[0] in aid


class DistractionPattern(BasePattern):
    """Rapid context-switching: more than 5 `APP_SWITCH` events inside a trailing
    2-minute window (blueprint F2, D-10). Re-arms once the switch rate settles
    back to <= 2 in the window.

    B13-A: the signal attaches the **distinct `app:<id>` nodes** thrashed in the
    window (`distinct` was already computed for the reason string and discarded).
    No `edges` — the `app --part_of--> domain` edge is owned by the `APP_SWITCH`
    classification path; re-emitting it would overwrite the key and reset any
    Hebbian `weight` (`_add_edge_unsafe` keys on `(source, target, relation)`).

    Known ceiling (D-19, §4): on a small graph L4's Jaccard-novelty gate (> 0.8)
    drops every distraction signal after the first — the same handful of apps
    recur. B13-B widens the app vocabulary; the ceiling is asserted intentional
    in the tests."""

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
        # B14: a browser-tab switch is a real context switch — key the distinct
        # set on (app_id, webapp) so Gmail -> Reddit -> YouTube counts as three.
        distinct: list[tuple[str, str | None]] = []
        for snapshot in recent:
            app_id = _str_field(snapshot, "app_id")
            if not app_id:
                continue
            webapp = _str_field(snapshot, "webapp") or None
            key = (app_id, webapp)
            if key not in distinct:
                distinct.append(key)
        confidence = _clamp01(0.5 + 0.1 * (count - self._max_switches))
        reason = (
            f"{count} app switches in {self._window_seconds / 60:.0f} min "
            f"({len(distinct)} distinct)"
        )
        specs = tuple(
            NodeSpec(
                node_id=f"webapp:{webapp}" if webapp else f"app:{app_id}",
                node_type=NodeType.WEBAPP if webapp else NodeType.APP,
                label=webapp or app_id,
            )
            for app_id, webapp in distinct
        )
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=confidence,
            source_snapshots=tuple(recent),
            node_specs=specs,
            reason=reason,
        )


class MemoryPressurePattern(BasePattern):
    """Sustained memory pressure (B13-B1, D-19).

    `_hit` when `system.mem_percent` z-scores above `z_threshold` **or**
    `mem_available_mb` drops below `floor_mb`; must hold for `sustain_seconds`
    (default 180 — shorter than HighLoad's 300 because a memory ceiling is a
    slower, more meaningful event than a CPU spike). Edge-triggered; re-arms once
    the z-score falls below 1.0 **and** available climbs back over the floor.

    Attaches the memory-heavy `app:<id>` nodes from the concurrent `process`
    census (B13-B2) with their resource attributes; attaches nothing until that
    census exists. Emits `HIGH_LOAD` — L4/L5 already consume it — and the pattern
    name in `PATTERN_DETECTED` keeps it distinct (no schema change, D-19)."""

    signal_type: ClassVar[SignalType] = SignalType.HIGH_LOAD
    collectors: ClassVar[tuple[str, ...]] = ("system", "process")

    def __init__(
        self,
        *,
        poll_seconds: float,
        z_threshold: float = _MEM_PRESSURE_Z,
        floor_mb: float = _MEM_PRESSURE_FLOOR_MB,
        sustain_seconds: float = _MEM_PRESSURE_SUSTAIN_SECONDS,
    ) -> None:
        self._min_run = max(1, math.ceil(sustain_seconds / max(1.0, poll_seconds)))
        self._z_threshold = z_threshold
        self._floor_mb = floor_mb
        self._firing = False

    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        system = windows.get("system", ())
        if not system:
            return None
        if self._firing:
            if self._cleared(system[-1], baselines):
                self._firing = False
            return None
        run = self._trailing_run(system, baselines)
        if len(run) < self._min_run:
            return None
        self._firing = True
        return self._draft(run, windows, baselines)

    def _hit(self, snapshot: MetricSnapshot, baselines: BaselineLookup) -> bool:
        avail = _num(snapshot, "mem_available_mb")
        if avail is not None and avail < self._floor_mb:
            return True
        pct = _num(snapshot, "mem_percent")
        if pct is None:
            return False
        return baselines.zscore("system", "mem_percent", pct) > self._z_threshold

    def _cleared(self, snapshot: MetricSnapshot, baselines: BaselineLookup) -> bool:
        avail = _num(snapshot, "mem_available_mb")
        pct = _num(snapshot, "mem_percent")
        below_floor = avail is not None and avail < self._floor_mb
        hot = (
            pct is not None
            and baselines.zscore("system", "mem_percent", pct) >= _MEM_PRESSURE_CLEAR_Z
        )
        return not below_floor and not hot

    def _trailing_run(
        self, window: Sequence[MetricSnapshot], baselines: BaselineLookup
    ) -> list[MetricSnapshot]:
        run: list[MetricSnapshot] = []
        for snapshot in reversed(window):
            if not self._hit(snapshot, baselines):
                break
            run.append(snapshot)
        run.reverse()
        return run

    def _draft(
        self,
        run: Sequence[MetricSnapshot],
        windows: Mapping[str, Sequence[MetricSnapshot]],
        baselines: BaselineLookup,
    ) -> SignalDraft:
        pcts = [p for p in (_num(s, "mem_percent") for s in run) if p is not None]
        mean_pct = sum(pcts) / len(pcts) if pcts else 0.0
        z = baselines.zscore("system", "mem_percent", mean_pct)
        avails = [a for a in (_num(s, "mem_available_mb") for s in run) if a is not None]
        min_avail = min(avails) if avails else None
        margin = _clamp01(0.5 + 0.1 * max(0.0, z - self._z_threshold))
        if min_avail is not None and min_avail < self._floor_mb:
            margin = _clamp01(margin + 0.2)
        specs = _heavy_app_specs(_census_rows(windows.get("process", ())), run[-1].timestamp)
        minutes = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0
        free = f", {min_avail:.0f} MB free" if min_avail is not None else ""
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=margin,
            source_snapshots=tuple(run),
            node_specs=specs,
            reason=f"mem {mean_pct:.0f}% (z {z:.1f}){free} for ~{minutes:.0f} min",
        )


class HeavyAppStartedPattern(BasePattern):
    """A new memory-heavy app group appeared (B13-B4, D-19(e)).

    Edge-triggered on *appearance*: fires once when an app name is in the current
    `process` census (i.e. already ≥ the grouped-RSS threshold) but was not in
    the previous one. Re-arms per app when it drops out of the census. During
    real use this fires on "launched a VM", "opened Blender", "Docker came up" —
    every firing widens the graph's input surface. `confidence = 0.5` — "something
    started", not "something's wrong". Emits `WORKING_SET_CHANGE`."""

    signal_type: ClassVar[SignalType] = SignalType.WORKING_SET_CHANGE
    collectors: ClassVar[tuple[str, ...]] = ("process",)

    def __init__(self) -> None:
        self._seen: frozenset[str] = frozenset()
        self._primed = False

    def evaluate(
        self, windows: Mapping[str, Sequence[MetricSnapshot]], baselines: BaselineLookup
    ) -> SignalDraft | None:
        process = windows.get("process", ())
        if not process:
            return None
        rows = _census_rows(process)
        current = frozenset(str(r.get("name", "")) for r in rows if r.get("name"))

        if not self._primed:
            # The first census is the baseline — everything in it is "already
            # running", not "just started". Arm, do not fire.
            self._seen = current
            self._primed = True
            return None

        appeared = current - self._seen
        self._seen = current
        if not appeared:
            return None

        now = process[-1].timestamp
        new_rows = [r for r in rows if str(r.get("name", "")) in appeared]
        specs = _heavy_app_specs(new_rows, now, cap=len(new_rows) or 1)
        names = ", ".join(
            f"{s.label} ({dict(s.attributes).get('ram_mb', 0.0):.0f} MB)" for s in specs
        )
        return SignalDraft(
            signal_type=self.signal_type,
            confidence=0.5,
            source_snapshots=(process[-1],),
            node_specs=specs,
            reason=f"{len(specs)} heavy app(s) started: {names}",
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
        MemoryPressurePattern(
            poll_seconds=system_poll,
            z_threshold=config.mem_pressure_z,
            floor_mb=config.mem_pressure_floor_mb,
            sustain_seconds=config.mem_pressure_sustain_seconds,
        ),
        HeavyAppStartedPattern(),
    ]
