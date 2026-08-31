"""B2.5 · ActivityCollector — real idle/activity edges (D-9).

Driven by `FakeIdleSource` — no compositor, no pywayland. The live Wayland path
(`WaylandIdleSource`) is covered by `tests/integration/` + the spike.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import CollectorError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.sensing.activity.collector import ActivityCollector
from neuropaca.sensing.activity.idle import FakeIdleSource, IdleCallback, IdleTransition
from neuropaca.sensing.activity.window import FakeWindowSource


def _collect(sink: list[Event]) -> Callable[[Event], object]:
    async def handler(event: Event) -> None:
        sink.append(event)

    return handler


async def _running_bus() -> EventBus:
    bus = EventBus.get_instance()
    await bus.start()
    return bus


async def _started(
    bus: EventBus,
    source: FakeIdleSource,
    *,
    window: FakeWindowSource | None = None,
    **cfg: object,
) -> ActivityCollector:
    collector = ActivityCollector(
        bus,
        Config(inference_backend="fake", **cfg),
        idle_source=source,
        window_source=window or FakeWindowSource(),
    )
    await collector.initialize()
    await collector.start()
    return collector


# ----------------------------------------------------------------- happy path


async def test_idle_then_active_publishes_the_two_edges() -> None:
    bus = await _running_bus()
    source = FakeIdleSource()
    collector = await _started(bus, source, idle_threshold_seconds=300)

    idle: list[Event] = []
    active: list[Event] = []
    bus.subscribe(EventType.IDLE_DETECTED, _collect(idle))
    bus.subscribe(EventType.ACTIVITY_DETECTED, _collect(active))

    source.emit(IdleTransition.IDLE)
    source.emit(IdleTransition.ACTIVE)
    await bus.join()

    assert len(idle) == 1
    assert idle[0].payload == {"source": "wayland", "idle_seconds": 300.0}
    assert idle[0].source == "sensing.activity"
    assert len(active) == 1
    assert active[0].payload["source"] == "wayland"
    assert active[0].payload["idle_seconds"] >= 0.0

    await collector.stop()
    await bus.stop()


async def test_edges_are_edge_triggered() -> None:
    bus = await _running_bus()
    source = FakeIdleSource()
    collector = await _started(bus, source)

    idle: list[Event] = []
    active: list[Event] = []
    bus.subscribe(EventType.IDLE_DETECTED, _collect(idle))
    bus.subscribe(EventType.ACTIVITY_DETECTED, _collect(active))

    source.emit(IdleTransition.IDLE)
    source.emit(IdleTransition.IDLE)  # repeat — ignored
    source.emit(IdleTransition.ACTIVE)
    source.emit(IdleTransition.ACTIVE)  # repeat — ignored
    await bus.join()

    assert len(idle) == 1
    assert len(active) == 1
    assert collector.health().ok is True

    await collector.stop()
    await bus.stop()


async def test_active_before_any_idle_is_a_noop() -> None:
    bus = await _running_bus()
    source = FakeIdleSource()
    collector = await _started(bus, source)

    active: list[Event] = []
    bus.subscribe(EventType.ACTIVITY_DETECTED, _collect(active))
    source.emit(IdleTransition.ACTIVE)
    await bus.join()
    assert active == []

    await collector.stop()
    await bus.stop()


# --------------------------------------------------------------- degraded path


class _BrokenSource:
    def start(self, on_transition: IdleCallback) -> None:
        raise CollectorError("no compositor")

    def stop(self) -> None:  # pragma: no cover - never started
        raise AssertionError("stop() on a source that never started")


async def test_source_that_cannot_start_self_disables_without_crashing() -> None:
    bus = await _running_bus()
    errors: list[Event] = []
    bus.subscribe(EventType.SYSTEM_ERROR, _collect(errors))

    collector = ActivityCollector(
        bus,
        Config(inference_backend="fake"),
        idle_source=_BrokenSource(),
        window_source=_BrokenSource(),
    )
    await collector.initialize()
    await collector.start()  # must not raise
    await bus.join()

    assert collector.is_running is True  # module up, just inert
    assert collector.health().ok is True
    assert any(e.payload["module"] == "sensing.activity.idle" for e in errors)
    assert any(e.payload["module"] == "sensing.activity.window" for e in errors)
    assert all(e.payload["severity"] == "collector-disabled" for e in errors)

    await collector.stop()  # must not call the broken sources' stop()
    await bus.stop()


# ------------------------------------------------------------------ APP_SWITCH


async def test_app_switch_fires_on_focused_app_id_change() -> None:
    bus = await _running_bus()
    idle, window = FakeIdleSource(), FakeWindowSource()
    collector = await _started(bus, idle, window=window)

    switches: list[Event] = []
    bus.subscribe(EventType.APP_SWITCH, _collect(switches))

    window.emit("md.Obsidian", "notes")
    window.emit("md.Obsidian", "notes 2")  # same app_id — no event
    window.emit("brave-browser", "web")
    await bus.join()

    assert [e.payload["app_id"] for e in switches] == ["md.Obsidian", "brave-browser"]
    assert switches[0].payload["previous_app_id"] is None
    assert switches[1].payload["previous_app_id"] == "md.Obsidian"
    assert switches[1].payload["title"] == "web"
    assert collector.health().detail.endswith("2 switches")

    await collector.stop()
    await bus.stop()


async def test_stop_is_idempotent_and_stops_the_source() -> None:
    bus = await _running_bus()
    source = FakeIdleSource()
    collector = await _started(bus, source)
    assert source.started is True

    await collector.stop()
    assert source.started is False
    await collector.stop()  # again — no error

    await bus.stop()


def test_fake_source_emit_before_start_raises() -> None:
    with pytest.raises(RuntimeError):
        FakeIdleSource().emit(IdleTransition.IDLE)
