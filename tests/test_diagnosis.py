"""B3 · Diagnosis (L3) — patterns, MetricBaseline, and SignalCorrelator.

Patterns are pure and synchronous, so most of this file drives them directly
with hand-built `MetricSnapshot`s — no bus, no clock, no graph. The correlator
tests use a real `EventBus` + `GraphMemory` (`tmp_path`), still no sleeping
(rules.md §8): L3 is purely event-driven.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.diagnosis.patterns import (
    BasePattern,
    HighLoadPattern,
    IdlePattern,
    build_patterns,
)
from neuropaca.diagnosis.signal import MetricBaseline, SignalDraft
from neuropaca.sensing.snapshot import MetricSnapshot

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _NoBaseline:
    def zscore(self, collector: str, metric: str, value: float) -> float:
        return 0.0


def _sys(cpu: float, *, t: int = 0, **extra: object) -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="system",
        timestamp=_BASE + timedelta(seconds=t * 60),
        data={"cpu_percent": cpu, **extra},
    )


def _fs(paths: list[str], *, t: int = 0) -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="filesystem",
        timestamp=_BASE + timedelta(seconds=t * 60),
        data={"changed_paths": paths, "change_count": len(paths)},
    )


def _collect(sink: list[Event]) -> Callable[[Event], object]:
    async def handler(event: Event) -> None:
        sink.append(event)

    return handler


# ------------------------------------------------------------------- HighLoad


def test_high_load_fires_after_five_sustained_high_samples() -> None:
    pattern = HighLoadPattern(poll_seconds=60.0)
    window = tuple(_sys(96.0, t=i) for i in range(5))
    draft = pattern.evaluate({"system": window, "filesystem": ()}, _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.HIGH_LOAD
    assert 0.5 <= draft.confidence <= 1.0
    assert len(draft.source_snapshots) == 5


def test_high_load_silent_on_moderate_load() -> None:
    pattern = HighLoadPattern(poll_seconds=60.0)
    window = tuple(_sys(55.0, t=i) for i in range(12))
    assert pattern.evaluate({"system": window, "filesystem": ()}, _NoBaseline()) is None


def test_high_load_silent_until_the_run_is_long_enough() -> None:
    pattern = HighLoadPattern(poll_seconds=60.0)
    window = tuple(_sys(99.0, t=i) for i in range(4))  # one short
    assert pattern.evaluate({"system": window, "filesystem": ()}, _NoBaseline()) is None


def test_high_load_fires_once_per_episode_then_rearms() -> None:
    pattern = HighLoadPattern(poll_seconds=60.0)
    nb = _NoBaseline()
    run = [_sys(97.0, t=i) for i in range(5)]
    assert pattern.evaluate({"system": tuple(run), "filesystem": ()}, nb) is not None

    run.append(_sys(98.0, t=5))  # still hot — no repeat
    assert pattern.evaluate({"system": tuple(run), "filesystem": ()}, nb) is None

    run.append(_sys(20.0, t=6))  # drops — clears, still no draft
    assert pattern.evaluate({"system": tuple(run), "filesystem": ()}, nb) is None

    run.extend(_sys(95.0, t=7 + i) for i in range(5))  # new episode
    assert pattern.evaluate({"system": tuple(run), "filesystem": ()}, nb) is not None


def test_high_load_related_nodes_come_from_filesystem_activity() -> None:
    pattern = HighLoadPattern(poll_seconds=60.0)
    system = tuple(_sys(96.0, t=i) for i in range(5))
    fs = (
        _fs(["/home/u/src/a.py", "/home/u/src/a.py", "/home/u/src/b.py"], t=1),
        _fs(["/home/u/src/a.py"], t=3),
    )
    draft = pattern.evaluate({"system": system, "filesystem": fs}, _NoBaseline())
    assert draft is not None
    ids = [spec.node_id for spec in draft.node_specs]
    assert ids[0] == "file:/home/u/src/a.py"  # most frequent first
    assert set(ids) == {"file:/home/u/src/a.py", "file:/home/u/src/b.py"}
    assert all(spec.node_type is NodeType.FILE for spec in draft.node_specs)


def test_high_load_confidence_scales_up_with_a_hot_baseline() -> None:
    class _HotZ:
        def zscore(self, collector: str, metric: str, value: float) -> float:
            return 4.0

    window = tuple(_sys(95.0, t=i) for i in range(5))
    cold = HighLoadPattern(poll_seconds=60.0).evaluate(
        {"system": window, "filesystem": ()}, _NoBaseline()
    )
    hot = HighLoadPattern(poll_seconds=60.0).evaluate({"system": window, "filesystem": ()}, _HotZ())
    assert cold is not None and hot is not None
    assert hot.confidence > cold.confidence


# ----------------------------------------------------------------------- Idle


def test_idle_fires_after_the_threshold_of_low_samples() -> None:
    pattern = IdlePattern(idle_threshold_seconds=300, poll_seconds=60.0)
    window = tuple(_sys(2.0, t=i) for i in range(5))
    draft = pattern.evaluate({"system": window}, _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.IDLE
    assert draft.confidence >= 0.7
    assert "min" in draft.reason


def test_idle_silent_while_cpu_is_active() -> None:
    pattern = IdlePattern(idle_threshold_seconds=300, poll_seconds=60.0)
    window = tuple(_sys(45.0, t=i) for i in range(10))
    assert pattern.evaluate({"system": window}, _NoBaseline()) is None


def test_idle_rearms_only_after_cpu_returns_to_active() -> None:
    pattern = IdlePattern(idle_threshold_seconds=300, poll_seconds=60.0)
    nb = _NoBaseline()
    run = [_sys(1.0, t=i) for i in range(5)]
    assert pattern.evaluate({"system": tuple(run)}, nb) is not None

    run.append(_sys(3.0, t=5))  # still idle, below ACTIVE — no rearm, no repeat
    assert pattern.evaluate({"system": tuple(run)}, nb) is None

    run.append(_sys(40.0, t=6))  # active again — clears
    assert pattern.evaluate({"system": tuple(run)}, nb) is None

    run.extend(_sys(2.0, t=7 + i) for i in range(5))
    assert pattern.evaluate({"system": tuple(run)}, nb) is not None


# ------------------------------------------------------------- MetricBaseline


def test_metric_baseline_zscore_is_zero_until_warm() -> None:
    baseline = MetricBaseline(window=10)
    assert baseline.zscore(100.0) == 0.0  # no samples
    baseline.observe(1.0)
    baseline.observe(1.0)
    assert baseline.zscore(100.0) == 0.0  # zero spread
    for _ in range(6):
        baseline.observe(1.0)
    baseline.observe(3.0)
    assert baseline.zscore(3.0) > 0.0


def test_metric_baseline_window_is_bounded() -> None:
    baseline = MetricBaseline(window=3)
    for value in (1.0, 1.0, 1.0, 100.0, 100.0, 100.0):
        baseline.observe(value)
    assert abs(baseline.mean() - 100.0) < 1e-9


# ---------------------------------------------------------------- correlator


async def _correlator(tmp_path, config: Config, **kw) -> tuple[SignalCorrelator, EventBus]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    correlator = SignalCorrelator(bus, config, graph, **kw)
    await correlator.initialize()
    await correlator.start()
    return correlator, bus


def _metric(snapshot: MetricSnapshot) -> Event:
    return Event(
        event_type=EventType.METRIC_COLLECTED,
        source=f"sensing.{snapshot.collector_name}",
        payload={"snapshot": snapshot},
    )


async def test_correlator_publishes_signal_and_pattern_detected_on_high_load(tmp_path) -> None:
    correlator, bus = await _correlator(tmp_path, Config(inference_backend="fake"))
    signals: list[Event] = []
    patterns: list[Event] = []
    mem: list[Event] = []
    bus.subscribe(EventType.SIGNAL_CORRELATED, _collect(signals))
    bus.subscribe(EventType.PATTERN_DETECTED, _collect(patterns))
    bus.subscribe(EventType.MEMORY_UPDATED, _collect(mem))

    for i in range(5):
        bus.publish(_metric(_sys(97.0, t=i)))
    await bus.join()

    assert len(signals) == 1
    signal = signals[0].payload["signal"]
    assert signal.signal_type is SignalType.HIGH_LOAD
    assert 0.0 <= signal.confidence <= 1.0
    assert signal.related_node_ids == ()  # no filesystem activity
    assert [e.payload["pattern"] for e in patterns] == ["HighLoadPattern"]
    assert mem == []  # MEMORY_UPDATED only when nodes were written

    await correlator.stop()
    await bus.stop()


async def test_correlator_upserts_file_nodes_and_never_resets_score(tmp_path) -> None:
    correlator, bus = await _correlator(tmp_path, Config(inference_backend="fake"))
    graph = correlator._graph
    await graph.add_node(
        "file:/w/a.py", NodeType.FILE, {"label": "a.py", "relevance_score": 7.5, "access_count": 3}
    )
    signals: list[Event] = []
    mem: list[Event] = []
    bus.subscribe(EventType.SIGNAL_CORRELATED, _collect(signals))
    bus.subscribe(EventType.MEMORY_UPDATED, _collect(mem))

    bus.publish(_metric(_fs(["/w/a.py", "/w/new.py"], t=2)))
    for i in range(5):
        bus.publish(_metric(_sys(98.0, t=i)))
    await bus.join()

    assert len(signals) == 1
    related = signals[0].payload["signal"].related_node_ids
    assert "file:/w/a.py" in related and "file:/w/new.py" in related

    kept = graph.get_node("file:/w/a.py")
    assert kept is not None
    assert kept.relevance_score == 7.5  # preserved by upsert
    assert kept.access_count == 4  # bumped
    created = graph.get_node("file:/w/new.py")
    assert created is not None and created.label == "new.py"

    assert len(mem) == 1
    assert mem[0].payload["operation"] == "signal_correlate"
    assert set(mem[0].payload["node_ids"]) == set(related)

    await correlator.stop()
    await bus.stop()


async def test_correlator_windows_are_bounded_by_config(tmp_path) -> None:
    config = Config(inference_backend="fake", correlation_window_seconds=180)  # ceil(180/60)+1 = 4
    correlator, bus = await _correlator(tmp_path, config)
    for i in range(25):
        bus.publish(_metric(_sys(50.0, t=i)))
    await bus.join()
    assert len(correlator._windows["system"]) == 4

    await correlator.stop()
    await bus.stop()


async def test_a_fifth_pattern_registers_without_touching_the_correlator(tmp_path) -> None:
    class DiskPressurePattern(BasePattern):
        signal_type = SignalType.FILE_ACTIVITY
        collectors = ("system",)

        def evaluate(self, windows, baselines) -> SignalDraft | None:
            window = windows.get("system", ())
            if window and float(window[-1].data.get("disk_percent", 0.0)) > 95.0:
                return SignalDraft(
                    signal_type=self.signal_type,
                    confidence=0.9,
                    source_snapshots=(window[-1],),
                    reason="disk almost full",
                )
            return None

    correlator, bus = await _correlator(
        tmp_path, Config(inference_backend="fake"), patterns=[DiskPressurePattern()]
    )
    signals: list[Event] = []
    bus.subscribe(EventType.SIGNAL_CORRELATED, _collect(signals))

    bus.publish(_metric(_sys(10.0, t=0, disk_percent=99.1)))
    await bus.join()

    assert len(signals) == 1
    assert signals[0].payload["signal"].signal_type is SignalType.FILE_ACTIVITY

    await correlator.stop()
    await bus.stop()


async def test_correlator_handler_survives_a_malformed_payload(tmp_path) -> None:
    correlator, bus = await _correlator(tmp_path, Config(inference_backend="fake"))
    errors: list[Event] = []
    bus.subscribe(EventType.SYSTEM_ERROR, _collect(errors))

    bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="x", payload={}))
    bus.publish(
        Event(event_type=EventType.METRIC_COLLECTED, source="x", payload={"snapshot": "nope"})
    )
    await bus.join()

    assert correlator._errors == 0  # ignored, not an error
    assert errors == []
    assert correlator.health().ok is True

    await correlator.stop()
    await bus.stop()


def test_correlator_takes_no_inference_backend() -> None:
    """B3 exit criterion — zero inference in L3. The correlator has no way to
    reach `BitNetRuntime`: it is not a constructor parameter."""
    params = inspect.signature(SignalCorrelator.__init__).parameters
    assert "bitnet_runtime" not in params
    assert "backend" not in params
    assert "runtime" not in params


def test_build_patterns_is_the_registry() -> None:
    patterns = build_patterns(Config(inference_backend="fake"))
    assert [type(p).__name__ for p in patterns] == [
        "HighLoadPattern",
        "IdlePattern",
        "FocusSessionPattern",  # B2.5b (D-10)
        "DistractionPattern",  # B2.5b (D-10)
    ]
