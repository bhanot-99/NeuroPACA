# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B13 · whole-pipeline replay (B13_PLAN.md §5 integration table).

Synthetic `METRIC_COLLECTED` / `APP_SWITCH` events replayed one at a time through
a real `SignalCorrelator` (real `EventBus` + `GraphMemory`, no sleeping), and the
L3->L4 hop through a real `BitNetPlasticity` on `FakeInferenceBackend`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.sensing.snapshot import MetricSnapshot

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _Spy:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def signals(self) -> list[Signal]:
        return [e.payload["signal"] for e in self.events]


def _metric(collector: str, data: dict, *, t: float) -> Event:
    snap = MetricSnapshot(
        collector_name=collector, timestamp=_BASE + timedelta(seconds=t), data=data
    )
    return Event(
        event_type=EventType.METRIC_COLLECTED,
        source=f"sensing.{collector}",
        payload={"snapshot": snap},
    )


def _switch(app_id: str, *, t: float) -> Event:
    return Event(
        event_type=EventType.APP_SWITCH,
        source="sensing.activity",
        payload={"app_id": app_id, "title": "", "previous_app_id": None},
        timestamp=_BASE + timedelta(seconds=t),
    )


async def _correlator(tmp_path: Path) -> tuple[SignalCorrelator, EventBus, _Spy, _Spy]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    corr = SignalCorrelator(bus, Config(inference_backend="fake"), graph)
    await corr.initialize()
    await corr.start()
    sig, err = _Spy(), _Spy()
    bus.subscribe(EventType.SIGNAL_CORRELATED, sig)
    bus.subscribe(EventType.SYSTEM_ERROR, err)
    return corr, bus, sig, err


async def _feed(corr: SignalCorrelator, bus: EventBus, events: list[Event]) -> None:
    for ev in events:
        if ev.event_type is EventType.APP_SWITCH:
            await corr.on_app_switch(ev)
        else:
            await corr.on_metric_event(ev)
        await bus.join()


# ------------------------------------------------------------- B13-A pipeline


async def test_distraction_signal_round_trips_and_upserts_the_apps(tmp_path: Path) -> None:
    corr, bus, sig, err = await _correlator(tmp_path)
    try:
        await _feed(corr, bus, [_switch(f"app{i}", t=i * 12) for i in range(6)])
        assert [s.signal_type for s in sig.signals] == [SignalType.DISTRACTION]
        related = sig.signals[0].related_node_ids
        assert set(related) == {f"app:app{i}" for i in range(6)}
        for nid in related:
            assert corr._graph.get_node(nid) is not None
        assert err.events == []
    finally:
        await corr.stop()
        await bus.stop()


async def test_idle_signal_attaches_the_last_active_app(tmp_path: Path) -> None:
    corr, bus, sig, _err = await _correlator(tmp_path)
    try:
        events: list[Event] = [_switch("com.system76.CosmicTerm", t=0)]
        events += [_metric("system", {"cpu_percent": 2.0}, t=i * 60) for i in range(6)]
        await _feed(corr, bus, events)
        idle = [s for s in sig.signals if s.signal_type is SignalType.IDLE]
        assert idle and idle[0].related_node_ids == ("app:com.system76.CosmicTerm",)
    finally:
        await corr.stop()
        await bus.stop()


# ------------------------------------------------------- B13-B1/B4 pipeline


async def test_memory_pressure_cites_heavy_apps_and_writes_ram_to_the_graph(
    tmp_path: Path,
) -> None:
    corr, bus, sig, err = await _correlator(tmp_path)
    try:
        events: list[Event] = [
            _metric(
                "process",
                {
                    "processes": [
                        {
                            "name": "brave",
                            "rss_mb": 2048.0,
                            "cpu_percent": 3.0,
                            "proc_count": 12,
                            "running_seconds": 3600.0,
                        },
                        {
                            "name": "code",
                            "rss_mb": 1536.0,
                            "cpu_percent": 1.0,
                            "proc_count": 4,
                            "running_seconds": 1800.0,
                        },
                    ],
                    "group_count": 2,
                },
                t=0,
            )
        ]
        # sustained low available memory -> fires without needing a warm baseline
        events += [
            _metric(
                "system",
                {"cpu_percent": 30.0, "mem_percent": 96.0, "mem_available_mb": 400.0},
                t=60 + i * 60,
            )
            for i in range(4)
        ]
        await _feed(corr, bus, events)
        loads = [s for s in sig.signals if s.signal_type is SignalType.HIGH_LOAD]
        assert loads, "memory pressure should have fired a HIGH_LOAD signal"
        assert set(loads[0].related_node_ids) == {"app:brave", "app:code"}
        brave = corr._graph.get_node("app:brave")
        assert brave is not None and brave.ram_mb == 2048.0
        assert brave.first_seen_at is not None
        assert err.events == []
    finally:
        await corr.stop()
        await bus.stop()


async def test_heavy_app_started_emits_working_set_change(tmp_path: Path) -> None:
    corr, bus, sig, _err = await _correlator(tmp_path)
    try:
        events = [
            _metric(
                "process",
                {
                    "processes": [
                        {
                            "name": "brave",
                            "rss_mb": 1200.0,
                            "cpu_percent": 0.0,
                            "proc_count": 8,
                            "running_seconds": 5000.0,
                        }
                    ],
                    "group_count": 1,
                },
                t=0,
            ),
            _metric(
                "process",
                {
                    "processes": [
                        {
                            "name": "brave",
                            "rss_mb": 1200.0,
                            "cpu_percent": 0.0,
                            "proc_count": 8,
                            "running_seconds": 5060.0,
                        },
                        {
                            "name": "qemu-system-x86",
                            "rss_mb": 4096.0,
                            "cpu_percent": 40.0,
                            "proc_count": 1,
                            "running_seconds": 30.0,
                        },
                    ],
                    "group_count": 2,
                },
                t=60,
            ),
        ]
        await _feed(corr, bus, events)
        wsc = [s for s in sig.signals if s.signal_type is SignalType.WORKING_SET_CHANGE]
        assert wsc and wsc[0].related_node_ids == ("app:qemu-system-x86",)
        node = corr._graph.get_node("app:qemu-system-x86")
        assert node is not None and node.ram_mb == 4096.0
    finally:
        await corr.stop()
        await bus.stop()


# ------------------------------------------------------------- L3 -> L4 hop


async def _plasticity(
    tmp_path: Path, **cfg: object
) -> tuple[BitNetPlasticity, EventBus, GraphMemory]:
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    for nid in ("app:slack", "app:brave", "app:term"):
        await gm.upsert_node(nid, NodeType.APP, {"label": nid.split(":")[1]})
    mod = BitNetPlasticity(
        bus, Config(inference_backend="fake", **cfg), gm, BitNetRuntime.get_instance()
    )
    await mod.initialize()
    await mod.start()
    return mod, bus, gm


def _distraction_signal(nodes: tuple[str, ...]) -> Event:
    sig = Signal(
        signal_type=SignalType.DISTRACTION,
        confidence=0.85,
        related_node_ids=nodes,
        source_snapshots=(MetricSnapshot("activity", _BASE, {}),),
        reason="6 app switches in 2 min (3 distinct)",
    )
    return Event(EventType.SIGNAL_CORRELATED, source="diagnosis", payload={"signal": sig})


async def _sink(seen: list[Event]):
    async def handler(event: Event) -> None:
        seen.append(event)

    return handler


async def test_a_distraction_signal_now_produces_an_insight(tmp_path: Path) -> None:
    mod, bus, _gm = await _plasticity(tmp_path)
    seen: list[Event] = []
    bus.subscribe(EventType.INSIGHT_GENERATED, await _sink(seen))
    try:
        await mod.on_signal_event(_distraction_signal(("app:slack", "app:brave", "app:term")))
        await bus.join()
        assert mod._generated == 1
        assert seen[0].payload["insight"].traces_to_evidence()
    finally:
        await mod.stop()
        await bus.stop()


async def test_novelty_gate_shuts_down_repeat_distraction_on_a_small_graph(tmp_path: Path) -> None:
    """The documented B13-A ceiling: same handful of apps recurring -> L4's
    Jaccard-novelty gate (> 0.8) drops every distraction after the first. B13-B's
    wider app vocabulary is what lifts this."""
    mod, bus, _gm = await _plasticity(tmp_path)
    try:
        for _ in range(5):
            await mod.on_signal_event(_distraction_signal(("app:slack", "app:brave", "app:term")))
            await bus.join()
        assert mod._generated == 1
    finally:
        await mod.stop()
        await bus.stop()


# gen-ref: f0b79b4c
