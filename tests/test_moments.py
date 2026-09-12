# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A0 · `MomentComposer` — the welcome-back moment (VISION_PHASES.md, F1).

No test loads a real model (`FakeClock`, rules.md §8). Handlers are invoked
directly (the DMN test convention, `tests/test_idle.py`) rather than round-tripped
through the dispatch loop.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event, Moment
from neuropaca.interface.moments import MomentComposer
from neuropaca.learning.insight import Insight

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def _composer(tmp_path, clock=None, **cfg) -> tuple[MomentComposer, EventBus, GraphMemory]:
    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    composer = MomentComposer(
        bus, Config(inference_backend="fake", **cfg), gm, clock=clock or FakeClock(wall=_NOW)
    )
    await composer.initialize()
    await composer.start()
    return composer, bus, gm


def _insight(node_id: str, label: str, *, created_at: datetime = _NOW) -> Insight:
    return Insight(
        category="proactive",
        cited_node_ids=(node_id,),
        source_signal=SignalType.IDLE,  # unused by the composer, any member does
        confidence=1.0,
        snapshot_count=1,
        node_id=node_id,
        label=label,
        created_at=created_at,
    )


async def test_a_full_spell_proposes_one_grounded_welcome_back(tmp_path) -> None:
    composer, bus, gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    await gm.add_node(
        "idle:q1",
        NodeType.IDLE_THOUGHT,
        {"label": "why was app:code busy?", "relevance_score": 5.0},
    )
    proposed: list[Event] = []
    actions: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))
    bus.subscribe(EventType.ACTION_PROPOSAL, _collect(actions))

    await composer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await composer.on_insight_generated(
        Event(
            event_type=EventType.INSIGHT_GENERATED,
            source="idle",
            payload={"insight": _insight("idle:q1", "why was app:code busy?")},
        )
    )
    await composer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 25 * 60.0})
    )
    await bus.join()

    assert len(proposed) == 1
    moment = proposed[0].payload["moment"]
    assert isinstance(moment, Moment)
    assert moment.kind == "welcome_back"
    assert "why was app:code busy?" in moment.text
    assert "Code" in moment.text
    assert set(moment.evidence) == {"idle:q1", "app:code"}

    assert len(actions) == 1
    kwargs = actions[0].payload["kwargs"]
    assert actions[0].payload["action_type"] == "notification"
    assert kwargs["text"] == moment.text
    assert set(kwargs["node_ids"]) == {"idle:q1", "app:code"}
    await bus.stop()


async def test_spell_shorter_than_minimum_produces_no_moment(tmp_path) -> None:
    composer, bus, gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
    )
    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await composer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 5 * 60.0})
    )
    await bus.join()

    assert proposed == []
    assert composer._dropped_below_min == 1
    await bus.stop()


async def test_no_thought_still_yields_the_where_you_were_line(tmp_path) -> None:
    composer, bus, gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    await gm.add_node("webapp:gmail", NodeType.WEBAPP, {"label": "Gmail"})
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_app_switch(
        Event(
            event_type=EventType.APP_SWITCH,
            payload={"app_id": "brave", "webapp": "gmail"},
        )
    )
    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await composer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 25 * 60.0})
    )
    await bus.join()

    assert len(proposed) == 1
    moment = proposed[0].payload["moment"]
    assert moment.evidence == ("webapp:gmail",)
    assert "Gmail" in moment.text
    assert "wondered" not in moment.text
    await bus.stop()


async def test_neither_half_grounded_is_silence_not_a_hollow_greeting(tmp_path) -> None:
    composer, bus, _gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    # A switch to an app the graph never actually got a node for (e.g. L3
    # hasn't classified it yet) must not be cited as evidence for a node that
    # does not exist.
    await composer.on_app_switch(
        Event(event_type=EventType.APP_SWITCH, payload={"app_id": "ghost", "webapp": None})
    )
    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await composer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 25 * 60.0})
    )
    await bus.join()

    assert proposed == []
    assert composer._dropped_ungrounded == 1
    await bus.stop()


async def test_daily_cap_holds(tmp_path) -> None:
    clock = FakeClock(wall=_NOW)
    composer, bus, gm = await _composer(
        tmp_path, clock=clock, welcome_min_idle_minutes=20, welcome_daily_cap=2
    )
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    for _ in range(3):
        await composer.on_app_switch(
            Event(event_type=EventType.APP_SWITCH, payload={"app_id": "code", "webapp": None})
        )
        await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
        await composer.on_activity_detected(
            Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 25 * 60.0})
        )
    await bus.join()

    assert len(proposed) == 2
    assert composer._dropped_capped == 1
    await bus.stop()


async def test_highest_relevance_thought_wins_ties_go_to_newest(tmp_path) -> None:
    composer, bus, gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    await gm.add_node("idle:low", NodeType.IDLE_THOUGHT, {"label": "low", "relevance_score": 1.0})
    await gm.add_node("idle:high", NodeType.IDLE_THOUGHT, {"label": "high", "relevance_score": 9.0})
    await gm.add_node(
        "idle:tie_old", NodeType.IDLE_THOUGHT, {"label": "tie old", "relevance_score": 9.0}
    )
    proposed: list[Event] = []
    bus.subscribe(EventType.MOMENT_PROPOSED, _collect(proposed))

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    for node_id, label, created_at in (
        ("idle:low", "low", _NOW),
        ("idle:tie_old", "tie old", _NOW),
        ("idle:high", "high", _NOW.replace(minute=30)),  # newer, same top score as tie_old
    ):
        await composer.on_insight_generated(
            Event(
                event_type=EventType.INSIGHT_GENERATED,
                source="idle",
                payload={"insight": _insight(node_id, label, created_at=created_at)},
            )
        )
    await composer.on_activity_detected(
        Event(event_type=EventType.ACTIVITY_DETECTED, payload={"idle_seconds": 25 * 60.0})
    )
    await bus.join()

    assert len(proposed) == 1
    assert "high" in proposed[0].payload["moment"].text
    assert proposed[0].payload["moment"].evidence == ("idle:high",)
    await bus.stop()


async def test_non_idle_insights_are_not_collected_for_the_spell(tmp_path) -> None:
    composer, bus, gm = await _composer(tmp_path, welcome_min_idle_minutes=20)
    await gm.add_node("idle:x", NodeType.IDLE_THOUGHT, {"label": "x", "relevance_score": 5.0})

    await composer.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await composer.on_insight_generated(
        Event(
            event_type=EventType.INSIGHT_GENERATED,
            source="diagnosis",  # L4, not L6 — must not be treated as an idle thought
            payload={"insight": _insight("idle:x", "x")},
        )
    )

    assert composer._spell_thoughts == []
    await bus.stop()


# gen-ref: 2c653bb5
