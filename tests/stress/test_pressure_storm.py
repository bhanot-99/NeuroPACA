"""Stress · B7 — L5 under a 5 000-signal storm (D-14).

The B7 exit criteria have to hold under load, not just in a two-event unit test.
This fires 5 000 correlated signals across 200 nodes with randomised confidence
and proves four things at once:

- **a single source never opens the high tier** — every event here comes from
  L3, and the corroboration set test refuses all 5 000 of them;
- **the pressure map stays bounded** — it can never exceed the number of distinct
  nodes, whatever the event count, and decay evicts what fades;
- **the bus is not flooded** — hysteresis means a hot node announces its crossing
  once, so `PRESSURE_THRESHOLD_REACHED` count is bounded by the node count, not
  the event count;
- **decay stays exact under load** — after 10 minutes of silence every surviving
  entry is under 1 % of the storm's peak, and ten minutes later the eviction
  floor has cleared the map entirely.
"""

from __future__ import annotations

import random

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.drive.pressure import PressureAccumulator

pytestmark = pytest.mark.stress

_EVENTS = 5_000
_NODES = 200


async def test_pressure_under_a_signal_storm(tmp_path) -> None:
    rng = random.Random(20260901)
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    clock = FakeClock()
    drive = PressureAccumulator(bus, Config(inference_backend="fake"), graph, clock=clock)
    await drive.initialize()
    await drive.start()

    crossings: list[str] = []

    async def spy(event: Event) -> None:
        crossings.append(event.payload["tier"])

    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)

    try:
        for i in range(_EVENTS):
            node_id = f"app:n{i % _NODES}"
            bus.publish(
                Event(
                    event_type=EventType.SIGNAL_CORRELATED,
                    source="diagnosis",
                    payload={
                        "signal": Signal(
                            signal_type=SignalType.HIGH_LOAD,
                            confidence=rng.uniform(0.5, 1.0),
                            related_node_ids=(node_id,),
                            source_snapshots=(),
                            reason="storm",
                        )
                    },
                )
            )
            if i % 500 == 0:
                await bus.join()
        await bus.join()

        tracked = drive.pressure_map
        peak = max(tracked.values())

        assert len(tracked) <= _NODES  # bounded by distinct nodes, not by events
        assert "high" not in crossings  # one source, 5 000 spikes, still shut
        assert 0 < len(crossings) <= _NODES  # one announcement per hot node

        # Ten minutes of silence: every entry is under 1 % of the storm's peak.
        await clock.advance(600)
        after_ten = drive.pressure_map
        assert max(after_ten.values()) < 0.01 * peak

        # Ten more, and the eviction floor has cleared the map entirely — a
        # storm leaves no residue to combine with tomorrow's signals.
        await clock.advance(600)
        assert drive.pressure_map == {}
        assert drive.health().ok is True
    finally:
        await drive.stop()
        await bus.stop()
