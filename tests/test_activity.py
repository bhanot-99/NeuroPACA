# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B2.5 · ActivityCollector — real idle/activity edges (D-9).

Driven by `FakeIdleSource` — no compositor, no pywayland. The live Wayland path
(`WaylandIdleSource`) is covered by `tests/integration/` + the spike.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

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
    window.emit("md.Obsidian", "notes 2")  # same app_id, not a browser — no event
    window.emit("brave-browser", "some blog - Brave")
    await bus.join()

    assert [e.payload["app_id"] for e in switches] == ["md.Obsidian", "brave-browser"]
    assert switches[0].payload["previous_app_id"] is None
    assert switches[1].payload["previous_app_id"] == "md.Obsidian"
    # the raw title is NOT in the payload — only an allowlisted `webapp` label
    assert "title" not in switches[1].payload
    assert switches[1].payload["webapp"] is None
    assert collector.health().detail.endswith("2 switches")

    await collector.stop()
    await bus.stop()


async def test_browser_tab_switches_fire_with_the_webapp_label() -> None:
    bus = await _running_bus()
    idle, window = FakeIdleSource(), FakeWindowSource()
    collector = await _started(bus, idle, window=window)

    switches: list[Event] = []
    bus.subscribe(EventType.APP_SWITCH, _collect(switches))

    window.emit("brave-browser", "Inbox (351) - me@gmail.com - Gmail - Brave")
    window.emit("brave-browser", "Inbox (352) - me@gmail.com - Gmail - Brave")  # tick, no event
    window.emit("brave-browser", "Rick Astley - YouTube - Brave")
    window.emit("brave-browser", "Inbox (352) - me@gmail.com - Gmail - Brave")  # re-focus
    await bus.join()

    labels = [e.payload["webapp"] for e in switches]
    assert labels == ["gmail", "youtube", "gmail"]
    assert switches[0].payload["webapp_domain"] == "domain:comms"
    assert switches[1].payload["previous_webapp"] == "gmail"
    # no fragment of any title leaked
    for e in switches:
        blob = repr(e.payload)
        assert "@gmail.com" not in blob and "351" not in blob and "Astley" not in blob

    await collector.stop()
    await bus.stop()


async def test_webapp_tracking_off_reproduces_b13_payload() -> None:
    bus = await _running_bus()
    idle, window = FakeIdleSource(), FakeWindowSource()
    collector = await _started(bus, idle, window=window, webapp_tracking_enabled=False)

    switches: list[Event] = []
    bus.subscribe(EventType.APP_SWITCH, _collect(switches))
    window.emit("brave-browser", "Inbox - Gmail - Brave")
    window.emit("brave-browser", "YouTube - Brave")  # same app_id, tracking off — no event
    await bus.join()

    assert len(switches) == 1
    assert switches[0].payload["webapp"] is None
    assert switches[0].payload["webapp_domain"] is None

    await collector.stop()
    await bus.stop()


async def test_health_turns_unhealthy_when_a_started_source_goes_deaf() -> None:
    # B15 · a source that started fine then died (poll-pump gave up) must drag
    # the module unhealthy — the alarm B7 never had.
    bus = await _running_bus()
    idle, window = FakeIdleSource(), FakeWindowSource()
    collector = await _started(bus, idle, window=window)
    assert collector.health().ok is True
    assert "window✓" in collector.health().detail

    window.started = False  # FakeWindowSource.is_alive tracks `started`

    dead = collector.health()
    assert dead.ok is False
    assert "window✗" in dead.detail and "idle✓" in dead.detail

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


# ---------------------------------------------- B15 · shared Wayland connection


class _FakeConn:
    """Stand-in for WaylandConnection — records wiring, never touches pywayland."""

    instances: ClassVar[list[_FakeConn]] = []

    def __init__(self) -> None:
        self.handlers: list[object] = []
        self.started = 0
        self.stopped = 0
        self.alive = True
        self.start_raises: Exception | None = None
        _FakeConn.instances.append(self)

    def add(self, handler: object) -> None:
        self.handlers.append(handler)

    def start(self) -> None:
        self.started += 1
        if self.start_raises is not None:
            raise self.start_raises

    def stop(self) -> None:
        self.stopped += 1

    @property
    def is_alive(self) -> bool:
        return self.alive


async def test_real_path_builds_one_shared_connection_with_both_handlers(monkeypatch) -> None:
    monkeypatch.setattr("neuropaca.sensing.activity.wayland_conn.WaylandConnection", _FakeConn)
    _FakeConn.instances.clear()
    bus = await _running_bus()
    collector = ActivityCollector(bus, Config(inference_backend="fake"))
    await collector.initialize()
    await collector.start()

    assert len(_FakeConn.instances) == 1, "not exactly one shared connection"
    conn = _FakeConn.instances[0]
    assert len(conn.handlers) == 2, "idle + window not both bound to the one connection"
    assert conn.started == 1, "conn.start() not called exactly once"
    detail = collector.health().detail
    assert "idle✓" in detail and "window✓" in detail
    assert collector.health().ok is True

    await collector.stop()
    assert conn.stopped == 1
    await bus.stop()


async def test_real_path_wayland_unavailable_disables_both_halves(monkeypatch) -> None:
    monkeypatch.setattr("neuropaca.sensing.activity.wayland_conn.WaylandConnection", _FakeConn)
    _FakeConn.instances.clear()
    _base_init = _FakeConn.__init__

    def _init(self: _FakeConn) -> None:
        _base_init(self)
        self.start_raises = CollectorError("compositor lacks the protocols")
        self.alive = False

    monkeypatch.setattr(_FakeConn, "__init__", _init)

    bus = await _running_bus()
    errors: list[Event] = []
    bus.subscribe(EventType.SYSTEM_ERROR, _collect(errors))
    collector = ActivityCollector(bus, Config(inference_backend="fake"))
    await collector.initialize()
    await collector.start()  # must not raise
    await bus.join()

    assert collector.is_running is True
    detail = collector.health().detail
    assert "idle✗" in detail and "window✗" in detail
    # both halves fail as one connection → exactly one error, labelled "wayland"
    assert len(errors) == 1
    assert errors[0].payload["module"] == "sensing.activity.wayland"
    assert errors[0].payload["severity"] == "collector-disabled"
    # a source that never started does not drag health unhealthy (unchanged tolerance)
    assert collector.health().ok is True

    await collector.stop()
    await bus.stop()


async def test_real_path_connection_dies_later_drags_health_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr("neuropaca.sensing.activity.wayland_conn.WaylandConnection", _FakeConn)
    _FakeConn.instances.clear()
    bus = await _running_bus()
    collector = ActivityCollector(bus, Config(inference_backend="fake"))
    await collector.initialize()
    await collector.start()
    assert collector.health().ok is True

    _FakeConn.instances[0].alive = False  # the pump gave up

    dead = collector.health()
    assert dead.ok is False
    assert "idle✗" in dead.detail and "window✗" in dead.detail

    await collector.stop()
    await bus.stop()


# gen-ref: dc030f10
