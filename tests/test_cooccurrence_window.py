# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""T7 · the correlator's Hebbian co-activation window.

Every `APP_SWITCH` wires the newly focused `app:` / `webapp:` node to whatever
was focused inside `coactivation_window_seconds`, so apps used in the same work
session accrue weight on the edge between them. Time is `time.monotonic()`, not
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

# app_ids the default app_map classifies (so a real `app:` node is created)
_ZED = "dev.zed.Zed"
_OBSIDIAN = "md.obsidian.Obsidian"
_SLACK = "com.slack.Slack"


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
    monkeypatch.setattr(correlator_mod.time, "monotonic", c)
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
        assert _cooccurrence_weight(graph, apps[0], apps[1]) == pytest.approx(0.05)  # hebbian_base
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
        assert _cooccurrence_weight(graph, apps[0], apps[1]) == pytest.approx(0.06)  # base + delta
    finally:
        await corr.stop()
        await bus.stop()


async def test_switch_outside_window_does_not_wire(tmp_path: Path, clock: _Clock) -> None:
    corr, bus, graph = await _correlator(tmp_path, coactivation_window_seconds=300.0)
    try:
        await corr.on_app_switch(_switch(_ZED, t=0))
        clock.advance(600)  # well past the 300 s window
        await corr.on_app_switch(_switch(_OBSIDIAN, t=600))

        apps = _app_nodes(graph)
        assert len(apps) == 2
        assert _cooccurrence_weight(graph, apps[0], apps[1]) is None
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
        assert _cooccurrence_weight(graph, "webapp:github", "webapp:linear") == pytest.approx(0.05)
    finally:
        await corr.stop()
        await bus.stop()
