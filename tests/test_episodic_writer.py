# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `EpisodicWriter` (VISION_PHASES.md)."""

from __future__ import annotations

from datetime import UTC, datetime

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.episodic_writer import EpisodicWriter
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event, Moment
from neuropaca.diagnosis.app_map import AppMap
from neuropaca.learning.insight import Insight

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


async def _writer(
    tmp_path, clock=None, app_map: AppMap | None = None
) -> tuple[EpisodicWriter, EventBus, EpisodeStore]:
    bus = EventBus.get_instance()
    await bus.start()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    cfg = Config(inference_backend="fake")
    writer = EpisodicWriter(bus, cfg, store, clock=clock or FakeClock(wall=_NOW), app_map=app_map)
    await writer.initialize()
    await writer.start()
    return writer, bus, store


async def test_focus_span_closes_on_next_switch(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    writer, bus, store = await _writer(tmp_path, clock)

    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await clock.advance(600.0)  # 10 minutes in "code"
    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "terminal", "webapp": None})
    )
    await store.flush()

    rows = await store.since(0)
    assert len(rows) == 1
    assert rows[0].kind == "focus_span"
    assert rows[0].subject == "app:code"
    assert (rows[0].t_end - rows[0].t_start).total_seconds() == 600.0
    await store.stop()
    await bus.stop()


async def test_focus_span_closes_on_idle(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    writer, bus, store = await _writer(tmp_path, clock)

    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await clock.advance(300.0)
    await writer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await clock.advance(1200.0)
    await writer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 1200.0})
    )
    await store.flush()

    rows = await store.since(0)
    kinds = {r.kind: r for r in rows}
    focus, idle = kinds["focus_span"], kinds["idle_span"]
    assert (focus.t_end - focus.t_start).total_seconds() == 300.0
    assert (idle.t_end - idle.t_start).total_seconds() == 1200.0
    await store.stop()
    await bus.stop()


async def test_insight_generated_is_recorded(tmp_path) -> None:
    writer, bus, store = await _writer(tmp_path)
    insight = Insight(
        category="proactive",
        cited_node_ids=("app:code",),
        source_signal=SignalType.IDLE,
        confidence=1.0,
        snapshot_count=1,
        node_id="idle:q1",
        label="why was app:code busy?",
        created_at=_NOW,
    )
    await writer.on_insight_generated(
        Event(event_type=EventType.INSIGHT_GENERATED, source="idle", payload={"insight": insight})
    )
    await store.flush()

    rows = await store.since(0)
    assert len(rows) == 1
    assert rows[0].kind == "insight"
    assert rows[0].subject == "idle:q1"
    assert rows[0].attrs["label"] == "why was app:code busy?"
    await store.stop()
    await bus.stop()


async def test_moment_feedback_is_recorded(tmp_path) -> None:
    writer, bus, store = await _writer(tmp_path)
    moment = Moment(
        kind="welcome_back",
        text="Welcome back.",
        evidence=("app:code",),
        value=1.0,
        context={},
        expires_at=_NOW,
    )
    await writer.on_moment_feedback(
        Event(
            event_type=EventType.MOMENT_FEEDBACK,
            payload={"moment": moment, "outcome": "dismissed"},
        )
    )
    await store.flush()

    rows = await store.since(0)
    assert len(rows) == 1
    assert rows[0].kind == "moment_feedback"
    assert rows[0].object == "dismissed"
    await store.stop()
    await bus.stop()


async def test_focus_span_records_the_app_map_domain(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    app_map = AppMap.from_dict({"app_id": {"code": "engineering"}})
    writer, bus, store = await _writer(tmp_path, clock, app_map)

    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await clock.advance(60.0)
    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "terminal", "webapp": None})
    )
    await store.flush()

    rows = await store.since(0)
    assert rows[0].subject == "app:code"
    assert rows[0].object == "domain:engineering"
    assert rows[0].attrs == {}
    await store.stop()
    await bus.stop()


async def test_webapp_focus_span_records_its_domain_and_browser(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    writer, bus, store = await _writer(tmp_path, clock)

    await writer.on_app_switch(
        Event(
            event_type=EventType.APP_SWITCH,
            payload={
                "app_id": "brave-browser",
                "webapp": "gmail",
                "webapp_domain": "domain:comms",
            },
        )
    )
    await clock.advance(120.0)
    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "brave-browser", "webapp": None})
    )
    await store.flush()

    rows = await store.since(0)
    assert rows[0].subject == "webapp:gmail"
    assert rows[0].object == "domain:comms"
    # "brave-browser" is aliased to "brave" in the real app_identity.default.toml
    assert rows[0].attrs == {"browser": "app:brave"}
    await store.stop()
    await bus.stop()


async def test_unresolvable_switch_still_closes_open_span(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    writer, bus, store = await _writer(tmp_path, clock)

    await writer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await clock.advance(60.0)
    await writer.on_app_switch(Event(event_type=EventType.APP_SWITCH, payload={}))
    await store.flush()

    rows = await store.since(0)
    assert len(rows) == 1
    assert rows[0].subject == "app:code"
    await store.stop()
    await bus.stop()


# gen-ref: 8f3c1a2d
