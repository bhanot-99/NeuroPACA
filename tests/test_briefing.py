# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · the briefing core (VISION_PHASES.md §3.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event, Moment
from neuropaca.interface.briefing import (
    BriefingComposer,
    BriefingItem,
    compose_briefing,
    select_greedy_submodular,
    should_brief_now,
)

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


async def _graph(tmp_path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


async def _store(tmp_path) -> EpisodeStore:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    return store


async def test_compose_briefing_picks_grounded_items_with_evidence(tmp_path) -> None:
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code", "relevance_score": 5.0})
    await gm.add_node("idle:q1", NodeType.IDLE_THOUGHT, {"label": "q1", "relevance_score": 5.0})

    yesterday = _NOW - timedelta(days=1)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", yesterday, yesterday + timedelta(hours=1))
    store.record_span(
        EpisodeKind.INSIGHT,
        "idle:q1",
        yesterday,
        yesterday,
        obj="proactive",
        attrs={"label": "why was app:code busy?"},
    )
    await store.flush()

    moment = await compose_briefing(
        gm,
        store,
        focus_history=[("app:code", _NOW)],
        now=_NOW,
        last_briefing_seq=0,
        config=Config(inference_backend="fake"),
    )

    assert moment is not None
    assert moment.kind == "briefing"
    assert set(moment.evidence) <= {"app:code", "idle:q1"}
    assert moment.evidence  # never an empty grounding
    await store.stop()


async def test_compose_briefing_returns_none_when_nothing_grounded(tmp_path) -> None:
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    moment = await compose_briefing(
        gm,
        store,
        focus_history=[],
        now=_NOW,
        last_briefing_seq=0,
        config=Config(inference_backend="fake"),
    )
    assert moment is None
    await store.stop()


async def test_greedy_submodular_respects_k(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for i in range(10):
        await gm.add_node(f"app:a{i}", NodeType.APP, {"label": f"a{i}"})
    candidates = [
        BriefingItem(anchor=f"app:a{i}", text=f"t{i}", evidence=(f"app:a{i}",), value=float(10 - i))
        for i in range(10)
    ]
    selected = select_greedy_submodular(candidates, gm, k=5, mu=0.5)
    assert len(selected) == 5


async def test_greedy_submodular_never_picks_near_duplicates(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:a", NodeType.APP, {"label": "a"})
    await gm.add_node("app:b", NodeType.APP, {"label": "b"})
    await gm.add_node("app:c", NodeType.APP, {"label": "c"})
    # a and b share every neighbour (near-duplicate); c is off on its own.
    await gm.add_node("shared", NodeType.CONCEPT, {"label": "shared"})
    await gm.add_edge("app:a", "shared", RelationType.RELATED_TO, weight=0.5)
    await gm.add_edge("app:b", "shared", RelationType.RELATED_TO, weight=0.5)
    await gm.add_node("only_c", NodeType.CONCEPT, {"label": "only_c"})
    await gm.add_edge("app:c", "only_c", RelationType.RELATED_TO, weight=0.5)

    candidates = [
        BriefingItem(anchor="app:a", text="a", evidence=("app:a",), value=10.0),
        BriefingItem(anchor="app:b", text="b", evidence=("app:b",), value=9.9),  # near-dup of a
        BriefingItem(anchor="app:c", text="c", evidence=("app:c",), value=5.0),  # diverse
    ]
    selected = select_greedy_submodular(candidates, gm, k=2, mu=20.0)
    anchors = {item.anchor for item in selected}
    assert anchors == {"app:a", "app:c"}  # b loses to c once a is already picked


def test_should_brief_now_first_ever_fires() -> None:
    cfg = Config(inference_backend="fake")
    assert should_brief_now(
        now=_NOW, last_briefing_at=None, last_activity_gap_seconds=0.0, config=cfg
    )


def test_should_brief_now_new_calendar_day_fires() -> None:
    last = _NOW - timedelta(days=1)
    cfg = Config(inference_backend="fake")
    assert should_brief_now(
        now=_NOW, last_briefing_at=last, last_activity_gap_seconds=0.0, config=cfg
    )


def test_should_brief_now_same_day_short_gap_is_silent() -> None:
    last = _NOW - timedelta(hours=1)
    cfg = Config(inference_backend="fake")
    assert not should_brief_now(
        now=_NOW, last_briefing_at=last, last_activity_gap_seconds=100.0, config=cfg
    )


def test_should_brief_now_long_idle_gap_fires_same_day() -> None:
    last = _NOW - timedelta(hours=1)
    cfg = Config(inference_backend="fake", briefing_idle_gap_hours=6.0)
    assert should_brief_now(
        now=_NOW, last_briefing_at=last, last_activity_gap_seconds=7 * 3600.0, config=cfg
    )


# --------------------------------------------------------------------------- BriefingComposer


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def _composer(
    tmp_path, clock=None, **cfg
) -> tuple[BriefingComposer, EventBus, GraphMemory, EpisodeStore]:
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    composer = BriefingComposer(
        bus, Config(inference_backend="fake", **cfg), gm, store, clock=clock or FakeClock(wall=_NOW)
    )
    await composer.initialize()
    await composer.start()
    return composer, bus, gm, store


async def test_module_proposes_a_moment_on_first_ever_trigger(tmp_path) -> None:
    composer, bus, gm, store = await _composer(tmp_path)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code", "relevance_score": 5.0})
    yesterday = _NOW - timedelta(days=1)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", yesterday, yesterday + timedelta(hours=1))
    await store.flush()

    proposed: list[Event] = []
    actions: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))
    bus.subscribe(EventType.ACTION_PROPOSAL, _collect(actions))

    await composer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await bus.join()

    assert len(proposed) == 1
    moment = proposed[0].payload["moment"]
    assert isinstance(moment, Moment) and moment.kind == "briefing"
    # A3: the guardian, not this composer, decides delivery now.
    assert actions == []
    await store.stop()
    await bus.stop()


async def test_module_stays_silent_with_nothing_grounded(tmp_path) -> None:
    composer, bus, _gm, store = await _composer(tmp_path)
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
    await bus.join()

    assert proposed == []
    assert composer._nothing_to_say == 1
    await store.stop()
    await bus.stop()


async def test_module_does_not_recheck_within_the_same_day_short_gap(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    composer, bus, gm, store = await _composer(tmp_path, clock)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code", "relevance_score": 5.0})
    yesterday = _NOW - timedelta(days=1)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", yesterday, yesterday + timedelta(hours=1))
    await store.flush()

    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
    await clock.advance(60.0)  # one minute later, same day, short gap
    await composer.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
    await bus.join()

    assert len(proposed) == 1  # the second trigger was a no-op, not a re-brief
    await store.stop()
    await bus.stop()


async def test_on_briefing_request_answers_with_a_fresh_report(tmp_path) -> None:
    composer, bus, gm, store = await _composer(tmp_path)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code", "relevance_score": 5.0})
    yesterday = _NOW - timedelta(days=1)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", yesterday, yesterday + timedelta(hours=1))
    await store.flush()

    reports: list[Event] = []
    bus.subscribe(EventType.BRIEFING_REPORT, _collect(reports))

    await composer.on_briefing_request(
        Event(event_type=EventType.BRIEFING_REQUEST, payload={"request_id": "r1"})
    )
    await bus.join()

    assert len(reports) == 1
    assert reports[0].payload["request_id"] == "r1"
    assert isinstance(reports[0].payload["moment"], Moment)
    await store.stop()
    await bus.stop()


async def test_on_briefing_request_reports_none_when_nothing_to_say(tmp_path) -> None:
    composer, bus, _gm, store = await _composer(tmp_path)
    reports: list[Event] = []
    bus.subscribe(EventType.BRIEFING_REPORT, _collect(reports))

    await composer.on_briefing_request(
        Event(event_type=EventType.BRIEFING_REQUEST, payload={"request_id": "r2"})
    )
    await bus.join()

    assert len(reports) == 1
    assert reports[0].payload["moment"] is None
    await store.stop()
    await bus.stop()


# gen-ref: a4e7b2c9
