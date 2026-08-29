"""B1 · EventBus — queue safety and subscriber isolation (Architecture.md §3.1, D-5).

Skips until `core/event_bus.py` exists; it lands with these tests in one commit
(rules.md §8).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("neuropaca.core.event_bus")

from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event


async def _started_bus() -> EventBus:
    bus = EventBus.get_instance()
    await bus.start()
    return bus


async def test_one_subscriber_raising_does_not_stop_the_other() -> None:
    bus = await _started_bus()
    calls: list[str] = []

    async def good(_ev: Event) -> None:
        calls.append("good")

    async def bad(_ev: Event) -> None:
        raise RuntimeError("subscriber boom")

    bus.subscribe(EventType.METRIC_COLLECTED, good)
    bus.subscribe(EventType.METRIC_COLLECTED, bad)

    bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="test"))
    await bus.join()

    assert calls == ["good"]
    await bus.stop()


async def test_a_raising_subscriber_produces_a_system_error_event() -> None:
    bus = await _started_bus()
    seen: list[Event] = []

    async def bad(_ev: Event) -> None:
        raise ValueError("kaboom")

    async def on_error(ev: Event) -> None:
        seen.append(ev)

    bus.subscribe(EventType.METRIC_COLLECTED, bad)
    bus.subscribe(EventType.SYSTEM_ERROR, on_error)

    bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="test"))
    await bus.join()

    assert len(seen) == 1
    payload = seen[0].payload
    assert seen[0].event_type is EventType.SYSTEM_ERROR
    assert "kaboom" in payload.get("exception", "")
    assert payload.get("severity") == "handler"
    await bus.stop()


async def test_a_failing_system_error_handler_does_not_recurse() -> None:
    bus = await _started_bus()

    async def bad_error_handler(_ev: Event) -> None:
        raise RuntimeError("error handler itself fails")

    bus.subscribe(EventType.SYSTEM_ERROR, bad_error_handler)
    bus.publish(Event(event_type=EventType.SYSTEM_ERROR, source="test", payload={"exception": "x"}))
    # Must settle — no infinite SYSTEM_ERROR storm.
    await asyncio.wait_for(bus.join(), timeout=1.0)
    await bus.stop()


async def test_publish_never_blocks_and_drops_when_full(monkeypatch, caplog) -> None:
    monkeypatch.setattr("neuropaca.core.event_bus._QUEUE_MAXSIZE", 3, raising=True)
    EventBus._reset_for_tests()
    bus = EventBus.get_instance()  # not started — nothing drains the queue

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="fill"))
        assert bus.queue_depth == 3
        # The 4th and 5th cannot fit; publish must return, not block or raise.
        bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="overflow"))
        bus.publish(Event(event_type=EventType.METRIC_COLLECTED, source="overflow"))

    assert bus.dropped_count == 2
    assert bus.queue_depth == 3
    assert any("queue full" in r.message.lower() for r in caplog.records)


async def test_unsubscribe_stops_delivery() -> None:
    bus = await _started_bus()
    hits: list[int] = []

    async def handler(_ev: Event) -> None:
        hits.append(1)

    bus.subscribe(EventType.USER_MESSAGE, handler)
    bus.unsubscribe(EventType.USER_MESSAGE, handler)
    bus.publish(Event(event_type=EventType.USER_MESSAGE, source="test"))
    await bus.join()

    assert hits == []
    await bus.stop()


async def test_reset_for_tests_gives_a_fresh_bus() -> None:
    async def noop(_ev: Event) -> None:
        return None

    first = EventBus.get_instance()
    first.subscribe(EventType.IDLE_DETECTED, noop)
    EventBus._reset_for_tests()
    second = EventBus.get_instance()
    assert second is not first
    assert second.queue_depth == 0
