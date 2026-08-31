"""B3 exit criterion · recorded-fixture replay (phases.md B3).

    "HighLoadPattern and IdlePattern each fire against a recorded fixture and
     stay silent against a negative fixture."

The three JSON traces under ``tests/fixtures/`` are produced by
``generate_b3_traces.py``. Here we load them straight off disk, deserialise them
back into :class:`MetricSnapshot` objects, and feed them one snapshot at a time
through a real :class:`SignalCorrelator` (real ``EventBus`` + ``GraphMemory``,
no sleeping — L3 is purely event-driven), spying on ``SIGNAL_CORRELATED``.

This supersedes the hand-built-window pattern tests in ``test_diagnosis.py`` for
the *exit-criterion* claim: those still prove the pattern algebra in isolation,
these prove the whole L3 pipeline against a serialised trace.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.diagnosis.signal import Signal
from neuropaca.sensing.snapshot import MetricSnapshot
from tests.fixtures import generate_b3_traces

_HIGH_LOAD_CPU = 90.0
_MIN_RUN = 5  # ceil(300 / 60) for both patterns at the default system poll


@pytest.fixture(scope="session", autouse=True)
def _ensure_traces() -> None:
    """Regenerate the traces if any is missing, so a fresh checkout still runs."""
    paths = (
        generate_b3_traces.TRACE_HIGHLOAD,
        generate_b3_traces.TRACE_IDLE,
        generate_b3_traces.TRACE_NOISE,
    )
    if not all(path.exists() for path in paths):
        generate_b3_traces.write_all()


# --------------------------------------------------------------------- loading


def _load_trace(path: Path) -> list[MetricSnapshot]:
    """Deserialise a trace file back into ``MetricSnapshot`` dataclasses."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        MetricSnapshot(
            collector_name=str(item["collector_name"]),
            timestamp=datetime.fromisoformat(str(item["timestamp"])),
            data=dict(item["data"]),
            anomaly_score=float(item["anomaly_score"]),
        )
        for item in raw["snapshots"]
    ]


def _metric_event(snapshot: MetricSnapshot) -> Event:
    return Event(
        event_type=EventType.METRIC_COLLECTED,
        source=f"sensing.{snapshot.collector_name}",
        payload={"snapshot": snapshot},
    )


# ----------------------------------------------------------------------- spy


class _EventSpy:
    """Async subscriber that records every event it is handed."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def signals(self) -> list[Signal]:
        return [event.payload["signal"] for event in self.events]


async def _make_pipeline(
    tmp_path: Path,
) -> tuple[SignalCorrelator, EventBus, _EventSpy, _EventSpy]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    correlator = SignalCorrelator(bus, Config(inference_backend="fake"), graph)
    await correlator.start()

    signal_spy = _EventSpy()
    error_spy = _EventSpy()
    bus.subscribe(EventType.SIGNAL_CORRELATED, signal_spy)
    bus.subscribe(EventType.SYSTEM_ERROR, error_spy)
    return correlator, bus, signal_spy, error_spy


async def _replay(
    correlator: SignalCorrelator, bus: EventBus, snapshots: list[MetricSnapshot]
) -> None:
    """Feed each snapshot synchronously, draining the bus after every one so the
    spy sees exactly what was published by that snapshot."""
    for snapshot in snapshots:
        await correlator.on_metric_event(_metric_event(snapshot))
        await bus.join()


# ------------------------------------------------------------------- HighLoad


async def test_highload_trace_fires_high_load_once_with_file_nodes(tmp_path: Path) -> None:
    correlator, bus, signals, errors = await _make_pipeline(tmp_path)
    try:
        await _replay(correlator, bus, _load_trace(generate_b3_traces.TRACE_HIGHLOAD))

        assert len(signals.events) == 1
        signal = signals.signals[0]
        assert signal.signal_type is SignalType.HIGH_LOAD

        expected = {f"file:{path}" for path in generate_b3_traces.HIGHLOAD_FILES}
        assert set(signal.related_node_ids) == expected

        # the specs were actually upserted into the graph, not just named
        for node_id in expected:
            assert correlator._graph.get_node(node_id) is not None

        assert errors.events == []
    finally:
        await correlator.stop()
        await bus.stop()


async def test_highload_does_not_fire_before_the_full_five_sample_run(tmp_path: Path) -> None:
    correlator, bus, signals, _errors = await _make_pipeline(tmp_path)
    try:
        snapshots = _load_trace(generate_b3_traces.TRACE_HIGHLOAD)
        sustained_system = 0
        fired_after: int | None = None

        for index, snapshot in enumerate(snapshots):
            await correlator.on_metric_event(_metric_event(snapshot))
            await bus.join()
            if (
                snapshot.collector_name == "system"
                and float(snapshot.data["cpu_percent"]) > _HIGH_LOAD_CPU
            ):
                sustained_system += 1
            if signals.events and fired_after is None:
                fired_after = index
                # the deque must have accumulated the full run first — never sooner
                assert sustained_system == _MIN_RUN
                assert len(correlator._windows["system"]) >= _MIN_RUN

        assert fired_after is not None
        assert len(signals.events) == 1
    finally:
        await correlator.stop()
        await bus.stop()


# ----------------------------------------------------------------------- Idle


async def test_idle_trace_fires_idle_once(tmp_path: Path) -> None:
    correlator, bus, signals, errors = await _make_pipeline(tmp_path)
    try:
        await _replay(correlator, bus, _load_trace(generate_b3_traces.TRACE_IDLE))

        assert len(signals.events) == 1
        assert signals.signals[0].signal_type is SignalType.IDLE
        assert errors.events == []
    finally:
        await correlator.stop()
        await bus.stop()


async def test_idle_does_not_fire_before_the_full_five_sample_run(tmp_path: Path) -> None:
    correlator, bus, signals, _errors = await _make_pipeline(tmp_path)
    try:
        snapshots = _load_trace(generate_b3_traces.TRACE_IDLE)

        for count, snapshot in enumerate(snapshots[: _MIN_RUN - 1], start=1):
            await correlator.on_metric_event(_metric_event(snapshot))
            await bus.join()
            assert signals.events == [], f"IDLE fired prematurely after {count} samples"

        await correlator.on_metric_event(_metric_event(snapshots[_MIN_RUN - 1]))
        await bus.join()
        assert len(signals.events) == 1
        assert signals.signals[0].signal_type is SignalType.IDLE
    finally:
        await correlator.stop()
        await bus.stop()


# ------------------------------------------------------------- Noise rejection


async def test_noise_trace_fires_nothing(tmp_path: Path) -> None:
    correlator, bus, signals, errors = await _make_pipeline(tmp_path)
    try:
        await _replay(correlator, bus, _load_trace(generate_b3_traces.TRACE_NOISE))

        assert signals.events == []
        assert errors.events == []
        assert correlator._errors == 0
    finally:
        await correlator.stop()
        await bus.stop()
