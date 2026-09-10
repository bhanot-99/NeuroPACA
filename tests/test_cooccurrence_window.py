# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""T7 / V-1 · the correlator's Hebbian co-activation window.

Every `APP_SWITCH` wires the newly focused `app:` / `webapp:` node to whatever
was *last active* inside `coactivation_window_seconds` — star-shaped, credit
falling linearly with the gap, weights on the saturating `w += rate * (1 - w)`
scale. Time is the correlator's `_now()` (suspend-aware `CLOCK_BOOTTIME`), not
the event timestamp — the tests drive it with a fake clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis import correlator as correlator_mod
from neuropaca.diagnosis.correlator import SignalCorrelator

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_RATE = Config(inference_backend="fake").hebbian_delta

# app_ids the default app_map classifies (so a real `app:` node is created)
_ZED = "dev.zed.Zed"
_OBSIDIAN = "md.obsidian.Obsidian"
_SLACK = "com.slack.Slack"
# an app_id no app_map rule covers
_UNMAPPED = "org.example.Unmapped"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(correlator_mod, "_now", c)
    return c


def _switch(
    app_id: str, webapp: str | None = None, domain: str | None = None, *, t: float
) -> Event:
    return Event(
        event_type=EventType.APP_SWITCH,
        source="sensing.activity",
        payload={"app_id": app_id, "webapp": webapp, "webapp_domain": domain},
        timestamp=_BASE + timedelta(seconds=t),
    )


async def _correlator(
    tmp_path: Path, **cfg: object
) -> tuple[SignalCorrelator, EventBus, GraphMemory]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    corr = SignalCorrelator(bus, Config(inference_backend="fake", **cfg), graph)
    await corr.initialize()
    await corr.start()
    return corr, bus, graph


def _app_nodes(graph: GraphMemory) -> list[str]:
    return [n for n in graph.node_ids if n.startswith("app:")]


def _id(corr: SignalCorrelator, raw: str) -> str:
    return corr._canon_app_id(raw)[0]


def _cooccurrence_weight(graph: GraphMemory, a: str, b: str) -> float | None:
    for src, dst in ((a, b), (b, a)):
        for e in graph.get_edges(src):
            if e.target_id == dst and e.relation is RelationType.RELATED_TO:
                return e.weight
    return None


async def test_two_switches_in_window_wire_the_pair(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(60)
        await corr.on_app_switch(_switch(_OBSIDIAN, t=60))

        apps = _app_nodes(graph)
        assert len(apps) == 2
        # Zed was in use right up to the switch -> full credit, one step from 0
        assert _cooccurrence_weight(graph, apps[0], apps[1]) == pytest.approx(_RATE)
    finally:
        await corr.stop()
        await bus.stop()


async def test_revisit_within_window_strengthens_the_edge(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(30)
        await corr.on_app_switch(_switch(_OBSIDIAN, t=30))
        clock.advance(30)
        await corr.on_app_switch(_switch(_ZED, t=60))  # back to Zed, Obsidian still warm

        apps = _app_nodes(graph)
        second = _RATE + _RATE * (1 - _RATE)  # saturating step, not + delta
        assert _cooccurrence_weight(graph, apps[0], apps[1]) == pytest.approx(second)
    finally:
        await corr.stop()
        await bus.stop()


async def test_weight_saturates_below_one(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        for i in range(200):  # switching back and forth all day
            clock.advance(40)
            await corr.on_app_switch(_switch(_ZED if i % 2 else _OBSIDIAN, t=40.0 * i))
        weight = _cooccurrence_weight(graph, _id(corr, _ZED), _id(corr, _OBSIDIAN))
        assert weight is not None and 0.99 < weight < 1.0
    finally:
        await corr.stop()
        await bus.stop()


async def test_alt_tab_flurry_counts_once_per_refractory(tmp_path: Path, clock: _Clock) -> None:
    """V-1 · ten rapid Zed<->Obsidian flips inside the refractory period are one
    co-use: a single step, and no graph write for the other nine."""
    corr, bus, graph = await _correlator(tmp_path, coactivation_refractory_seconds=30.0)
    try:
        for i in range(10):
            clock.advance(1)
            await corr.on_app_switch(_switch(_ZED if i % 2 else _OBSIDIAN, t=float(i)))
        pair = (_id(corr, _ZED), _id(corr, _OBSIDIAN))
        assert _cooccurrence_weight(graph, *pair) == pytest.approx(_RATE)

        clock.advance(30)  # refractory over -> the next flip steps again
        await corr.on_app_switch(_switch(_OBSIDIAN, t=40))
        assert _cooccurrence_weight(graph, *pair) == pytest.approx(_RATE + _RATE * (1 - _RATE))
    finally:
        await corr.stop()
        await bus.stop()


async def test_app_last_active_outside_window_does_not_wire(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path, coactivation_window_seconds=300.0)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(30)
        await corr.on_app_switch(_switch(_OBSIDIAN, t=30))  # Zed last active at 30
        clock.advance(400)  # Obsidian in use for 400 s
        await corr.on_app_switch(_switch(_SLACK, t=430))

        zed, obsidian, slack = (_id(corr, a) for a in (_ZED, _OBSIDIAN, _SLACK))
        assert _cooccurrence_weight(graph, zed, slack) is None  # 400 s gap > window
        assert _cooccurrence_weight(graph, obsidian, slack) == pytest.approx(_RATE)
    finally:
        await corr.stop()
        await bus.stop()


async def test_focus_left_overnight_does_not_wire_the_morning(
    tmp_path: Path, clock: _Clock
) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(10 * 3600)  # walked away; Zed stayed focused
        await corr.on_app_switch(_switch(_OBSIDIAN, t=36000))
        assert _cooccurrence_weight(graph, _id(corr, _ZED), _id(corr, _OBSIDIAN)) is None
    finally:
        await corr.stop()
        await bus.stop()


async def test_switch_wires_star_not_clique(tmp_path: Path, clock: _Clock) -> None:
    """V-1 · a switch to Slack wires Slack<->Zed and Slack<->Obsidian but must
    not re-bump Zed<->Obsidian — that re-bump on every switch is what grew the
    clique."""
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(10)
        await corr.on_app_switch(_switch(_OBSIDIAN, t=10))  # Zed last active at 10
        clock.advance(10)
        await corr.on_app_switch(_switch(_SLACK, t=20))

        zed, obsidian, slack = (_id(corr, a) for a in (_ZED, _OBSIDIAN, _SLACK))
        assert _cooccurrence_weight(graph, zed, obsidian) == pytest.approx(_RATE)  # untouched
        assert _cooccurrence_weight(graph, obsidian, slack) == pytest.approx(_RATE)
        # Zed was last active 10 s before Slack -> credit 1 - 10/300
        assert _cooccurrence_weight(graph, zed, slack) == pytest.approx(_RATE * (1 - 10 / 300))
    finally:
        await corr.stop()
        await bus.stop()


async def test_unmapped_app_joins_the_mesh(tmp_path: Path, clock: _Clock) -> None:
    """V-1 · an app with no `app_map` domain used to get no node from a focus
    event and so never co-activated with anything."""
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(30)
        await corr.on_app_switch(_switch(_UNMAPPED, t=30))

        unmapped = _id(corr, _UNMAPPED)
        assert graph.get_node(unmapped) is not None
        assert _cooccurrence_weight(graph, _id(corr, _ZED), unmapped) == pytest.approx(_RATE)
    finally:
        await corr.stop()
        await bus.stop()


async def test_excluded_dialog_neither_joins_nor_breaks_the_chain(
    tmp_path: Path, clock: _Clock
) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(10)
        await corr.on_app_switch(_switch("zenity", t=10))
        clock.advance(10)
        await corr.on_app_switch(_switch(_OBSIDIAN, t=20))

        assert graph.get_node(_id(corr, "zenity")) is None
        assert _cooccurrence_weight(graph, _id(corr, _ZED), _id(corr, _OBSIDIAN)) == (
            pytest.approx(_RATE)
        )
    finally:
        await corr.stop()
        await bus.stop()


async def test_no_self_edge_on_repeat_focus(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(5)
        await corr.on_app_switch(_switch(_ZED, t=5))
        (zed,) = _app_nodes(graph)
        assert _cooccurrence_weight(graph, zed, zed) is None
    finally:
        await corr.stop()
        await bus.stop()


async def test_coactivation_deque_is_bounded(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, _graph = await _correlator(tmp_path, coactivation_max_nodes=4)
    try:
        for i in range(10):
            await corr.on_app_switch(
                _switch("brave-browser", webapp=f"tab{i}", domain="domain:habits", t=float(i))
            )
            clock.advance(1)
        assert len(corr._coactive) <= 4
    finally:
        await corr.stop()
        await bus.stop()


async def test_webapp_focus_wires_the_webapp_node(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("brave-browser", "github", "domain:engineering", t=0))
        clock.advance(60)
        await corr.on_app_switch(_switch("brave-browser", "linear", "domain:projects", t=60))
        assert _cooccurrence_weight(graph, "webapp:github", "webapp:linear") == pytest.approx(_RATE)
    finally:
        await corr.stop()
        await bus.stop()


async def test_tab_never_wires_to_its_own_browser(tmp_path: Path, clock: _Clock) -> None:
    """V-1 · `webapp:youtube --part_of--> app:brave` is structure. Before, the
    co-activation bumped that PART_OF edge's weight instead of learning
    anything; now the pair is skipped outright."""
    corr, bus, graph = await _correlator(tmp_path)
    try:
        await corr.on_app_switch(_switch("brave-browser", t=0))  # unrecognised tab
        clock.advance(20)
        await corr.on_app_switch(_switch("brave-browser", "youtube", "domain:habits", t=20))

        brave = _id(corr, "brave-browser")
        assert _cooccurrence_weight(graph, "webapp:youtube", brave) is None
        part_of = [e for e in graph.get_edges("webapp:youtube") if e.target_id == brave]
        assert [e.weight for e in part_of] == [0.0]
    finally:
        await corr.stop()
        await bus.stop()


# gen-ref: 05780527
