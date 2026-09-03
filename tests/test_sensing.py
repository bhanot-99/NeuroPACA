"""B2 · Sensing (L2) — XMetricCollector, the collectors, and orchestrator wiring.

Deterministic: a `FakeCollector` + `FakeClock` + an inline `collect()` runner, so
no real threads, no real psutil, no sleeping (rules.md §8). One `@pytest.mark.
integration` test exercises the real `SystemMetricCollector`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator
from neuropaca.sensing.base_collector import BaseCollector
from neuropaca.sensing.collector_module import XMetricCollector
from neuropaca.sensing.collectors.system import SystemMetricCollector
from neuropaca.sensing.snapshot import MetricSnapshot


class FakeCollector(BaseCollector):
    def __init__(
        self,
        name: str = "system",
        *,
        interval: float = 60.0,
        data_sequence: list[dict[str, object]] | None = None,
        fail_always: bool = False,
    ) -> None:
        super().__init__(name, interval)
        self._data = data_sequence or [{"cpu_percent": 50.0}]
        self._fail_always = fail_always
        self._successes = 0
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def collect(self) -> MetricSnapshot:
        if self._fail_always:
            raise RuntimeError(f"{self.name} collect boom")
        idx = min(self._successes, len(self._data) - 1)
        self._successes += 1
        return MetricSnapshot(
            collector_name=self.name, timestamp=datetime.now(UTC), data=dict(self._data[idx])
        )


async def _inline(fn: Callable[[], MetricSnapshot]) -> MetricSnapshot:
    return fn()


def _module(
    bus: EventBus, clock: FakeClock, *, buffer_size: int = 720, max_failures: int = 3
) -> XMetricCollector:
    config = Config(
        inference_backend="fake", snapshot_buffer_size=buffer_size, max_failures=max_failures
    )
    return XMetricCollector(bus, config, clock=clock, runner=_inline)


def _collect(events: list[Event]) -> Callable[[Event], object]:
    async def handler(event: Event) -> None:
        events.append(event)

    return handler


async def _running_bus() -> EventBus:
    bus = EventBus.get_instance()
    await bus.start()
    return bus


# --------------------------------------------------------------------------- poll


async def test_poll_publishes_metric_collected_with_the_snapshot_payload() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock)
    module.register_collector(FakeCollector("system", data_sequence=[{"cpu_percent": 42.0}]))

    seen: list[Event] = []
    bus.subscribe(EventType.METRIC_COLLECTED, _collect(seen))

    await module.start()
    await clock.advance(60)
    await bus.join()

    assert len(seen) == 1
    snapshot = seen[0].payload["snapshot"]
    assert snapshot.collector_name == "system"
    assert snapshot.data["cpu_percent"] == 42.0
    assert snapshot.anomaly_score == 0.0
    assert len(module.snapshot_buffer) == 1

    await module.stop()
    await bus.stop()


async def test_ring_buffer_is_bounded() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock, buffer_size=3)
    module.register_collector(FakeCollector("system"))

    await module.start()
    for _ in range(6):
        await clock.advance(60)

    assert len(module.snapshot_buffer) == 3

    await module.stop()
    await bus.stop()


# ------------------------------------------------------------------- isolation


async def test_one_collector_failing_does_not_stop_the_others() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock, max_failures=3)
    good = FakeCollector("system", data_sequence=[{"cpu_percent": 50.0}])
    bad = FakeCollector("filesystem", fail_always=True)
    module.register_collector(good)
    module.register_collector(bad)

    metrics: list[Event] = []
    errors: list[Event] = []
    bus.subscribe(EventType.METRIC_COLLECTED, _collect(metrics))
    bus.subscribe(EventType.SYSTEM_ERROR, _collect(errors))

    await module.start()
    for _ in range(4):
        await clock.advance(60)
    await bus.join()

    assert len(metrics) >= 3  # the good collector kept publishing
    assert good.is_enabled is True
    assert bad.is_enabled is False  # self-disabled after max_failures
    assert any(e.payload["module"] == "sensing.filesystem" for e in errors)
    assert any(e.payload["severity"] == "collector-disabled" for e in errors)

    await module.stop()
    await bus.stop()


# ------------------------------------------------------------------ idle signal


async def test_system_cpu_drives_idle_then_activity_edges() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock)
    module.register_collector(
        FakeCollector(
            "system",
            data_sequence=[
                {"cpu_percent": 50.0},  # active
                {"cpu_percent": 3.0},  # -> IDLE_DETECTED
                {"cpu_percent": 2.0},  # still idle, no repeat
                {"cpu_percent": 25.0},  # -> ACTIVITY_DETECTED
            ],
        )
    )

    idle: list[Event] = []
    active: list[Event] = []
    bus.subscribe(EventType.IDLE_DETECTED, _collect(idle))
    bus.subscribe(EventType.ACTIVITY_DETECTED, _collect(active))

    await module.start()
    for _ in range(4):
        await clock.advance(60)
    await bus.join()

    assert len(idle) == 1
    assert idle[0].payload["source"] == "cpu"
    assert len(active) == 1
    assert active[0].payload["idle_seconds"] >= 0.0

    await module.stop()
    await bus.stop()


# -------------------------------------------------------------------- health


async def test_health_reports_up_collectors_and_buffer_fill() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock)
    c1 = FakeCollector("system")
    c2 = FakeCollector("filesystem")
    module.register_collector(c1)
    module.register_collector(c2)

    await module.start()
    c2.is_enabled = False
    report = module.health()

    assert report.ok is True  # one collector still up
    assert "1/2" in report.detail

    await module.stop()
    await bus.stop()


async def test_stop_cancels_poll_tasks_and_stops_collectors() -> None:
    bus = await _running_bus()
    clock = FakeClock()
    module = _module(bus, clock)
    collector = FakeCollector("system")
    module.register_collector(collector)

    await module.start()
    assert collector.started is True

    await module.stop()
    assert module.is_running is False
    assert collector.stopped is True

    metrics: list[Event] = []
    bus.subscribe(EventType.METRIC_COLLECTED, _collect(metrics))
    await clock.advance(600)
    await bus.join()
    assert metrics == []

    await bus.stop()


# ---------------------------------------------------------------- wiring


def test_build_modules_wires_system_and_optionally_filesystem(tmp_path) -> None:
    bus = EventBus.get_instance()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    runtime = BitNetRuntime.get_instance()

    modules = build_modules(Config(inference_backend="fake"), bus, graph, runtime)
    # L2 -> L3 -> L4 -> L5 -> L7 -> L6 -> L9 (B7, D-14; Architecture.md §10 A7)
    assert [m.name for m in modules] == [
        "sensing",
        "diagnosis",
        "learning",
        "drive",
        "action",
        "idle",
        "interface",
    ]
    assert isinstance(modules[0], XMetricCollector)
    assert [c.name for c in modules[0]._collectors] == ["system"]

    with_fs = build_modules(
        Config(inference_backend="fake", watch_paths=[str(tmp_path)]), bus, graph, runtime
    )
    assert [c.name for c in with_fs[0]._collectors] == ["system", "filesystem"]

    # B2.5: activity_enabled inserts ActivityCollector (L2) and stands the
    # CPU-derived idle stand-in down.
    with_activity = build_modules(
        Config(inference_backend="fake", activity_enabled=True), bus, graph, runtime
    )
    assert [m.name for m in with_activity] == [
        "sensing",
        "activity",
        "diagnosis",
        "learning",
        "drive",
        "action",
        "idle",
        "interface",
    ]
    assert with_activity[0]._emit_idle_from_cpu is False


async def test_orchestrator_runs_sensing_via_build_modules(tmp_path) -> None:
    config = Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "graph.json"),
        action_log_path=str(tmp_path / "actions.jsonl"),
        graph_save_interval_seconds=3600,
        interface_socket_path=str(tmp_path / "np.sock"),
    )
    orch = NeuroPACAOrchestrator(config, module_builder=build_modules)

    await orch.initialize()
    await orch.start()
    assert orch.is_running is True

    health = orch.health_check()
    assert health.ok is True
    assert any(m.name == "sensing" for m in health.modules)

    await orch.stop()
    assert orch.is_running is False


# ---------------------------------------------------------------- integration


@pytest.mark.integration
def test_system_metric_collector_reads_the_real_machine() -> None:
    snapshot = SystemMetricCollector().collect()
    assert snapshot.collector_name == "system"
    assert 0.0 <= snapshot.data["cpu_percent"] <= 100.0
    assert "mem_percent" in snapshot.data
    assert "disk_percent" in snapshot.data
    assert snapshot.anomaly_score == 0.0
