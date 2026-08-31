"""Stress · APP_SWITCH saturation — a window-manager glitch must not stall L3 (D-10).

A compositor bug can machine-gun focus events. This publishes 20 000 rapid-fire
`APP_SWITCH`es (two app_ids alternating) through a real `EventBus` into a real
`SignalCorrelator` and proves:

- **edge-triggered, not per-event** — `DistractionPattern` fires exactly once for
  one unbroken burst (it re-arms only when the switch rate falls back to <= 2 in
  a 2-minute window, which never happens mid-storm).
- **the synthetic "activity" deque is strictly bounded** — it pins at its
  config-derived `maxlen` (`ceil(1800 / 2) + 1 = 901`); old snapshots evict and
  the traced heap is flat from the 1 000th event to the 20 000th.
- **the loop never stalls** — a 10 ms probe running alongside the storm is never
  woken more than 50 ms late.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from pathlib import Path

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator

pytestmark = pytest.mark.stress

_STORM = 20_000
# Drain often — the publisher loop has no `await` between `publish()` calls, so a
# batch this size is the longest the dispatch loop runs before the probe gets a
# turn. 40 keeps that well under the 50 ms budget even on a loaded machine.
_DRAIN_EVERY = 40
_EXPECTED_DEQUE_MAXLEN = 901  # ceil(correlation_window_seconds 1800 / nominal 2 s poll) + 1
_LAG_LIMIT_MS = 50.0
_HEAP_DRIFT_LIMIT_KB = 512.0
_APPS = ("glitch.Flip", "glitch.Flop")  # unclassified -> never a FOCUS_SESSION
_RULES = Path(__file__).resolve().parents[2] / "data" / "app_map.default.toml"


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + 0.01
        await asyncio.sleep(0.01)
        samples.append((loop.time() - expected) * 1000.0)


async def test_app_switch_storm_fires_once_and_stays_bounded(tmp_path: Path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    correlator = SignalCorrelator(
        bus, Config(inference_backend="fake", app_map_path=str(_RULES)), graph
    )
    await correlator.initialize()
    await correlator.start()

    signals: list[SignalType] = []
    errors: list[Event] = []

    async def on_signal(event: Event) -> None:
        signals.append(event.payload["signal"].signal_type)

    async def on_error(event: Event) -> None:
        errors.append(event)

    bus.subscribe(EventType.SIGNAL_CORRELATED, on_signal)
    bus.subscribe(EventType.SYSTEM_ERROR, on_error)

    stop = asyncio.Event()
    lag: list[float] = []
    probe = asyncio.create_task(_probe(lag, stop))
    await asyncio.sleep(0.05)  # let the probe settle

    gc.collect()
    tracemalloc.start()
    heap_after_warmup = 0

    for i in range(_STORM):
        bus.publish(
            Event(
                event_type=EventType.APP_SWITCH,
                source="sensing.activity",
                payload={
                    "app_id": _APPS[i % 2],
                    "title": "",
                    "previous_app_id": _APPS[(i + 1) % 2],
                },
            )
        )
        if (i + 1) % _DRAIN_EVERY == 0:
            await bus.join()
            if i + 1 == 1_000:
                gc.collect()
                heap_after_warmup, _ = tracemalloc.get_traced_memory()

    await bus.join()
    heap_end, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    stop.set()
    probe.cancel()
    try:
        await probe
    except asyncio.CancelledError:
        pass

    # precondition — a dropped event would make the counts nondeterministic
    assert bus.dropped_count == 0, f"{bus.dropped_count} APP_SWITCH events were dropped"
    assert errors == [], f"handler raised: {[e.payload for e in errors]}"

    # edge-triggered: one unbroken burst -> exactly one DISTRACTION, no FOCUS_SESSION
    assert signals.count(SignalType.DISTRACTION) == 1, (
        f"got {signals.count(SignalType.DISTRACTION)} DISTRACTION signals"
    )
    assert SignalType.FOCUS_SESSION not in signals

    # the deque evicts — bounded regardless of how many events arrived
    activity_window = correlator._windows["activity"]
    assert activity_window.maxlen == _EXPECTED_DEQUE_MAXLEN
    assert len(activity_window) == _EXPECTED_DEQUE_MAXLEN

    drift_kb = (heap_end - heap_after_warmup) / 1024.0
    assert drift_kb < _HEAP_DRIFT_LIMIT_KB, (
        f"traced heap grew {drift_kb:.0f} KB across events 1 000 -> {_STORM} — the "
        "activity window is leaking"
    )

    # the storm never blocked the loop
    assert lag, "probe never sampled"
    assert max(lag) < _LAG_LIMIT_MS, f"max loop lag {max(lag):.1f} ms during the storm"

    await correlator.stop()
    await bus.stop()
