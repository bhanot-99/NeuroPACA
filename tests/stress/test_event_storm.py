"""Stress · EventBus queue saturation & dispatch recovery (rules.md §0, §2).

Invariants under test:
- ``publish()`` is fire-and-forget and never blocks, even at 50k calls/burst
- a full bounded queue *drops* (bumping ``dropped_count``); it never back-pressures
- once dispatch starts, every event still in the queue is delivered to every
  subscriber, and the loop keeps running for fresh traffic afterwards

Phase 1 measures the drop arithmetic deterministically: the dispatch loop is
**not started** during the burst, so ``dispatched == 0`` and the queue holds
exactly ``maxsize``. Phase 2 then proves graceful recovery with the loop live.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from neuropaca.core import event_bus as event_bus_mod
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event

pytestmark = pytest.mark.stress

_QUEUE_MAXSIZE = 1_000
_STORM = 50_000
_COLLECTORS = 10  # simulated collectors publishing concurrently
_SUBSCRIBERS = 5


def _lightweight_math() -> int:
    # stand-in for a pattern / action doing a little real work per event
    return sum(k * k for k in range(16))


async def test_event_storm_saturation_then_graceful_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_bus_mod, "_QUEUE_MAXSIZE", _QUEUE_MAXSIZE, raising=True)
    bus = EventBus.get_instance()
    assert bus._queue.maxsize == _QUEUE_MAXSIZE

    received = [0] * _SUBSCRIBERS

    def make_subscriber(index: int):
        async def subscriber(_event: Event) -> None:
            _lightweight_math()
            received[index] += 1

        return subscriber

    for i in range(_SUBSCRIBERS):
        bus.subscribe(EventType.METRIC_COLLECTED, make_subscriber(i))

    # ---- Phase 1 · the storm, with the dispatch loop deliberately NOT running --
    per_collector = _STORM // _COLLECTORS

    async def collector(name: str) -> int:
        for _ in range(per_collector):
            bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source=name))
        return per_collector

    start = time.perf_counter()
    counts = await asyncio.gather(*(collector(f"collector-{c}") for c in range(_COLLECTORS)))
    storm_seconds = time.perf_counter() - start

    published = sum(counts)
    assert published == _STORM
    # "never blocks": 50k `Event()` + `put_nowait` is CPU-bound but must not
    # *await* — a blocked publisher would take tens of seconds, not ~1-3 s. The
    # budget has CI headroom over the ~1 s warm run.
    assert storm_seconds < 8.0, f"publish() burst took {storm_seconds:.3f}s — it blocked"

    dispatched = sum(received)
    assert dispatched == 0, "dispatch ran during the burst — Phase 1 is not deterministic"
    assert bus.queue_depth == _QUEUE_MAXSIZE, "queue did not pin at maxsize"
    assert bus.dropped_count == _STORM - _QUEUE_MAXSIZE - dispatched

    # ---- Phase 2 · start dispatch, drain, prove recovery -----------------------
    await bus.start()
    await bus.join()

    assert bus.queue_depth == 0
    for i, got in enumerate(received):
        assert got == _QUEUE_MAXSIZE, f"subscriber {i} received {got}, expected {_QUEUE_MAXSIZE}"
    assert bus.dropped_count == _STORM - _QUEUE_MAXSIZE  # unchanged by draining

    # a second burst *with the loop live*, yielding so dispatch keeps up — the bus
    # recovers cleanly and drops nothing more
    recovered: list[Event] = []

    async def probe(event: Event) -> None:
        recovered.append(event)

    bus.subscribe(EventType.USER_MESSAGE, probe)
    for i in range(5_000):
        bus.publish(Event(event_type=EventType.USER_MESSAGE, source="post-storm"))
        if i % 100 == 0:
            await asyncio.sleep(0)  # let the dispatch loop drain the backlog
    await bus.join()

    assert bus.is_running is True
    assert bus.queue_depth == 0
    assert len(recovered) == 5_000
    assert bus.dropped_count == _STORM - _QUEUE_MAXSIZE  # still nothing extra dropped

    await bus.stop()
