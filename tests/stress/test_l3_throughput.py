"""Stress · L3 diagnosis pipeline compression (Architecture.md §5, D-8).

Feeds 30 days of 60-second telemetry (~43 200 snapshots — noise, periodic
high-load spikes, idle drops) straight through ``SignalCorrelator.on_metric_event``
in a tight loop and proves:

- pattern evaluation stays pure CPU and micro-second fast (no hidden I/O, no
  inference) — the whole month replays in well under the time budget
- the per-collector deque (``ceil(1800 / 60) + 1 = 31``) and the per-metric
  baseline strictly evict, so the Python heap is flat from snapshot 100 to
  snapshot 43 000 — no unbounded accumulation feeding B4's loop
"""

from __future__ import annotations

import gc
import random
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.sensing.snapshot import MetricSnapshot

pytestmark = pytest.mark.stress

_POLL_S = 60
_SNAPSHOTS = 30 * 24 * 60  # 43_200
_DRAIN_EVERY = 2_000
_TIME_BUDGET_S = 20.0  # generous for CI; a warm machine does this in ~1 s
_PER_SNAPSHOT_BUDGET_US = 400.0
_MEM_DRIFT_LIMIT_KB = 256.0
_MEASURE_LOW, _MEASURE_HIGH = 100, 43_000


def _cpu_percent(index: int, rng: random.Random) -> float:
    """A 4-hour behavioural cycle: an ~8-min high-load spike, an ~10-min idle
    drop, noise the rest of the time."""
    phase = index % (4 * 60)
    if phase < 8:
        return rng.uniform(91.0, 99.0)  # sustained > 90 -> HIGH_LOAD
    if 120 <= phase < 130:
        return rng.uniform(0.5, 4.5)  # sustained < 5 -> IDLE
    return rng.uniform(15.0, 78.0)  # noise, never sustained past a threshold


async def test_l3_replays_a_month_of_telemetry_fast_and_flat(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()

    async def drain(_event: Event) -> None:  # keep published signals from piling up
        return None

    for event_type in (
        EventType.SIGNAL_CORRELATED,
        EventType.PATTERN_DETECTED,
        EventType.MEMORY_UPDATED,
    ):
        bus.subscribe(event_type, drain)

    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    nodes_at_start = graph.node_count

    correlator = SignalCorrelator(bus, Config(inference_backend="fake"), graph)
    assert correlator._max_samples("system") == 31  # 30 min / 60 s + 1

    rng = random.Random(20260901)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem_low = mem_high = 0

    tracemalloc.start()
    start = time.perf_counter()
    for i in range(_SNAPSHOTS):
        snapshot = MetricSnapshot(
            collector_name="system",
            timestamp=base + timedelta(seconds=i * _POLL_S),
            data={"cpu_percent": _cpu_percent(i, rng)},
        )
        await correlator.on_metric_event(
            Event(
                event_type=EventType.METRIC_COLLECTED,
                source="sensing.system",
                payload={"snapshot": snapshot},
            )
        )
        if i % _DRAIN_EVERY == 0:
            await bus.join()
        if i == _MEASURE_LOW:
            gc.collect()
            mem_low = tracemalloc.get_traced_memory()[0]
        elif i == _MEASURE_HIGH:
            gc.collect()
            mem_high = tracemalloc.get_traced_memory()[0]
    elapsed = time.perf_counter() - start
    await bus.join()
    tracemalloc.stop()

    per_snapshot_us = elapsed / _SNAPSHOTS * 1e6

    # --- pure CPU, micro-second fast ----------------------------------------
    assert correlator._errors == 0
    assert elapsed < _TIME_BUDGET_S, (
        f"{_SNAPSHOTS} snapshots took {elapsed:.2f}s ({per_snapshot_us:.1f} us/snapshot)"
    )
    assert per_snapshot_us < _PER_SNAPSHOT_BUDGET_US, (
        f"{per_snapshot_us:.1f} us/snapshot — pattern eval is not pure CPU"
    )

    # --- deques strictly evict: window pinned at the cap, graph did not grow -
    assert len(correlator._windows["system"]) == 31
    assert len(correlator._baselines) == 1  # ("system", "cpu_percent") only
    assert graph.node_count == nodes_at_start  # high-load spikes carry no fs activity

    # --- heap flat across ~42 900 snapshots --------------------------------
    drift_kb = abs(mem_high - mem_low) / 1024.0
    assert drift_kb < _MEM_DRIFT_LIMIT_KB, (
        f"heap drifted {drift_kb:.1f} KB between snapshot {_MEASURE_LOW} and "
        f"{_MEASURE_HIGH} — something is accumulating"
    )

    await bus.stop()
