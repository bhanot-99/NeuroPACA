"""Stress · B4 — the dedicated inference executor must not stall the loop (D-11).

`BitNetRuntime.infer_async` holds `_inference_lock` and offloads the blocking
model call to its own single-worker `ThreadPoolExecutor` (rules.md §1). This
starts a mock inference that blocks its worker thread for 10 s, then blasts
10 000 `METRIC_COLLECTED` + `APP_SWITCH` events through a real `EventBus` into a
real `SignalCorrelator` while a 10 ms probe measures loop lag.

Proves: a 10 s blocking task on the executor is invisible to the event loop —
max lag stays < 50 ms — and L3 still ingests every one of the 10 000 events with
zero queue saturation.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.sensing.snapshot import MetricSnapshot

pytestmark = pytest.mark.stress

_METRICS = 5_000
_SWITCHES = 5_000
_DRAIN_EVERY = 50  # keep the 1000-slot queue far from saturation
_LAG_LIMIT_MS = 50.0
_INFER_BLOCK_S = 10.0
_RULES = Path(__file__).resolve().parents[2] / "data" / "app_map.default.toml"
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _TenSecondBackend:
    """A model whose `infer()` blocks its worker thread for 10 s."""

    def __init__(self) -> None:
        self._loaded = False
        self.infer_calls = 0

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        self.infer_calls += 1
        time.sleep(_INFER_BLOCK_S)
        return '{"cited_node_id": null, "insight_category": "routine"}'

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return 0.0


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + 0.01
        await asyncio.sleep(0.01)
        samples.append((loop.time() - expected) * 1000.0)


def _metric_event(i: int) -> Event:
    snap = MetricSnapshot(
        collector_name="system",
        timestamp=_BASE.replace(second=i % 60),
        data={"cpu_percent": 40.0 + (i % 10)},
    )
    return Event(
        event_type=EventType.METRIC_COLLECTED, source="sensing.system", payload={"snapshot": snap}
    )


def _switch_event(i: int) -> Event:
    return Event(
        event_type=EventType.APP_SWITCH,
        source="sensing.activity",
        payload={"app_id": f"app.{i % 4}", "title": "", "previous_app_id": None},
    )


async def test_10s_inference_does_not_stall_the_loop_during_a_10k_event_storm(
    tmp_path: Path,
) -> None:
    backend = _TenSecondBackend()
    runtime = BitNetRuntime(backend)
    await runtime.load_model_async()

    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    correlator = SignalCorrelator(
        bus, Config(inference_backend="fake", app_map_path=str(_RULES)), graph
    )
    await correlator.initialize()
    await correlator.start()

    errors: list[Event] = []

    async def on_error(event: Event) -> None:
        errors.append(event)

    bus.subscribe(EventType.SYSTEM_ERROR, on_error)

    stop = asyncio.Event()
    lag: list[float] = []
    probe = asyncio.create_task(_probe(lag, stop))
    await asyncio.sleep(0.05)

    # the 10 s blocking inference — running on the dedicated executor throughout
    infer_task = asyncio.create_task(runtime.infer_async("prompt", 48, 0.0, None))
    await asyncio.sleep(0.05)
    assert not infer_task.done()

    # interleave 10 000 events and drain frequently so nothing is dropped
    for i in range(max(_METRICS, _SWITCHES)):
        if i < _METRICS:
            bus.publish(_metric_event(i))
        if i < _SWITCHES:
            bus.publish(_switch_event(i))
        if (i + 1) % _DRAIN_EVERY == 0:
            await bus.join()
    await bus.join()

    assert not infer_task.done(), "the 10 s inference finished early — it was not actually blocking"

    stop.set()
    probe.cancel()
    infer_task.cancel()
    for task in (probe, infer_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    # (1) the blocking executor task never touched the loop
    assert lag, "probe never sampled"
    assert max(lag) < _LAG_LIMIT_MS, f"max loop lag {max(lag):.1f} ms during the 10 s inference"

    # (2) L3 ingested every event, no saturation, no error
    assert bus.dropped_count == 0, f"{bus.dropped_count} events dropped — queue saturated"
    assert errors == []
    assert correlator._errors == 0
    assert len(correlator._windows["system"]) == correlator._windows["system"].maxlen
    assert len(correlator._windows["activity"]) == correlator._windows["activity"].maxlen
    assert backend.infer_calls == 1

    await correlator.stop()
    await bus.stop()
