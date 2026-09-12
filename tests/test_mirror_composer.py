# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · `MirrorComposer` — the daemon-side driver over `core/mirror.py`
(VISION_PHASES.md §3.8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.mirror import MirrorResult
from neuropaca.core.models import Event, Moment
from neuropaca.interface.mirror_composer import MirrorComposer, build_mirror_moment, render_mirror

_EVENING = datetime(2026, 9, 15, 19, 0, tzinfo=UTC)  # a Tuesday, after the evening hour
_MORNING = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # after today's seeded activity, before evening


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def _composer(
    tmp_path, clock=None, **cfg
) -> tuple[MirrorComposer, EventBus, GraphMemory, EpisodeStore]:
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    composer = MirrorComposer(
        bus,
        Config(inference_backend="fake", **cfg),
        gm,
        store,
        clock=clock or FakeClock(wall=_EVENING),
    )
    await composer.initialize()
    await composer.start()
    return composer, bus, gm, store


async def _seed_unusual_day(gm, store, now: datetime) -> None:
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    await gm.add_node("app:spreadsheet", NodeType.APP, {"label": "Spreadsheet"})
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for age in range(1, 15):
        base = day_start - timedelta(days=age) + timedelta(hours=9)
        store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", base, base + timedelta(hours=2))
    today = day_start + timedelta(hours=9)
    store.record_span(
        EpisodeKind.FOCUS_SPAN, "app:spreadsheet", today, today + timedelta(hours=4)
    )
    await store.flush()


# ---------------------------------------------------------------------- render_mirror


def test_render_mirror_names_a_top_bucket_and_a_missing_one() -> None:
    result = MirrorResult(
        kl=1.0,
        surprising=True,
        top=[(("app:spreadsheet", 9), 0.3)],
        missing=[(("app:obsidian", 20), 0.2)],
    )

    class _FakeGraph:
        def display_name(self, node_id: str) -> str:
            return {"app:spreadsheet": "Spreadsheet", "app:obsidian": "Obsidian"}[node_id]

    text = render_mirror(_FakeGraph(), result)  # type: ignore[arg-type]
    assert "Spreadsheet" in text
    assert "Obsidian" in text
    assert "didn't open Obsidian" in text


# --------------------------------------------------------------------- build_mirror_moment


async def test_build_mirror_moment_is_grounded_and_none_on_an_ordinary_day(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    try:
        day_start = _EVENING.replace(hour=0, minute=0, second=0, microsecond=0)
        for age in range(1, 15):
            base = day_start - timedelta(days=age) + timedelta(hours=9)
            store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", base, base + timedelta(hours=1))
        today = day_start + timedelta(hours=9)
        store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", today, today + timedelta(hours=1))
        await store.flush()
        moment = await build_mirror_moment(
            gm, store, now=_EVENING, config=Config(inference_backend="fake")
        )
        assert moment is None
    finally:
        await store.stop()


async def test_build_mirror_moment_fires_and_is_grounded_on_a_surprising_day(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    try:
        await _seed_unusual_day(gm, store, _EVENING)
        moment = await build_mirror_moment(
            gm,
            store,
            now=_EVENING,
            config=Config(inference_backend="fake", mirror_kl_threshold=0.1),
        )
        assert isinstance(moment, Moment)
        assert moment.kind == "mirror"
        assert moment.evidence  # never an empty grounding
        assert all(gm.has_node(node_id) for node_id in moment.evidence)
    finally:
        await store.stop()


# --------------------------------------------------------------------------- MirrorComposer


async def test_module_proposes_a_moment_on_the_first_evening_idle(tmp_path) -> None:
    composer, bus, gm, store = await _composer(tmp_path, mirror_kl_threshold=0.1)
    await _seed_unusual_day(gm, store, _EVENING)

    proposed: list[Event] = []
    actions: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))
    bus.subscribe(EventType.ACTION_PROPOSAL, _collect(actions))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert len(proposed) == 1
    moment = proposed[0].payload["moment"]
    assert isinstance(moment, Moment) and moment.kind == "mirror"
    assert len(actions) == 1
    assert actions[0].payload["trigger"] == "mirror"
    await store.stop()
    await bus.stop()


async def test_module_stays_silent_before_the_evening_hour(tmp_path) -> None:
    clock = FakeClock(wall=_MORNING)
    composer, bus, gm, store = await _composer(tmp_path, clock, mirror_kl_threshold=0.1)
    await _seed_unusual_day(gm, store, _MORNING)

    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert proposed == []
    await store.stop()
    await bus.stop()


async def test_module_fires_at_most_once_per_calendar_day(tmp_path) -> None:
    clock = FakeClock(wall=_EVENING)
    composer, bus, gm, store = await _composer(tmp_path, clock, mirror_kl_threshold=0.1)
    await _seed_unusual_day(gm, store, _EVENING)

    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await clock.advance(3600.0)  # an hour later, same evening
    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert len(proposed) == 1  # the second trigger was a no-op, not a re-check
    await store.stop()
    await bus.stop()


async def test_module_stays_silent_on_an_ordinary_day(tmp_path) -> None:
    composer, bus, _gm, store = await _composer(tmp_path)
    day_start = _EVENING.replace(hour=0, minute=0, second=0, microsecond=0)
    for age in range(1, 15):
        base = day_start - timedelta(days=age) + timedelta(hours=9)
        store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", base, base + timedelta(hours=1))
    today = day_start + timedelta(hours=9)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", today, today + timedelta(hours=1))
    await store.flush()

    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert proposed == []
    assert composer._not_surprising == 1
    await store.stop()
    await bus.stop()


async def test_mirror_disabled_by_config_is_a_no_op(tmp_path) -> None:
    composer, bus, gm, store = await _composer(
        tmp_path, mirror_kl_threshold=0.1, mirror_enabled=False
    )
    await _seed_unusual_day(gm, store, _EVENING)

    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await bus.join()

    assert proposed == []
    await store.stop()
    await bus.stop()


async def test_on_mirror_request_answers_with_a_fresh_report(tmp_path) -> None:
    composer, bus, gm, store = await _composer(tmp_path, mirror_kl_threshold=0.1)
    await _seed_unusual_day(gm, store, _EVENING)

    reports: list[Event] = []
    bus.subscribe(EventType.MIRROR_REPORT, _collect(reports))

    await composer.on_mirror_request(
        Event(event_type=EventType.MIRROR_REQUEST, payload={"request_id": "r1"})
    )
    await bus.join()

    assert len(reports) == 1
    assert reports[0].payload["request_id"] == "r1"
    assert isinstance(reports[0].payload["moment"], Moment)
    await store.stop()
    await bus.stop()


async def test_on_mirror_request_reports_none_on_an_ordinary_day(tmp_path) -> None:
    composer, bus, _gm, store = await _composer(tmp_path)
    reports: list[Event] = []
    bus.subscribe(EventType.MIRROR_REPORT, _collect(reports))

    await composer.on_mirror_request(
        Event(event_type=EventType.MIRROR_REQUEST, payload={"request_id": "r2"})
    )
    await bus.join()

    assert len(reports) == 1
    assert reports[0].payload["moment"] is None
    await store.stop()
    await bus.stop()


async def test_on_mirror_request_works_even_before_the_evening_hour(tmp_path) -> None:
    """On-demand ignores the evening-hour gate entirely — a morning
    `neuropaca mirror` still gets a fresh answer, not a stale silence."""
    clock = FakeClock(wall=_MORNING)
    composer, bus, gm, store = await _composer(tmp_path, clock, mirror_kl_threshold=0.1)
    await _seed_unusual_day(gm, store, _MORNING)

    reports: list[Event] = []
    bus.subscribe(EventType.MIRROR_REPORT, _collect(reports))

    await composer.on_mirror_request(
        Event(event_type=EventType.MIRROR_REQUEST, payload={"request_id": "r3"})
    )
    await bus.join()

    assert len(reports) == 1
    assert isinstance(reports[0].payload["moment"], Moment)
    await store.stop()
    await bus.stop()
