# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · the deterministic full rebuild (VISION_PHASES.md, "graph-store consistency").

The parity test below is the load-bearing one: it runs the *live*
`SignalCorrelator` and `EpisodicWriter` off the same synthetic switch stream,
then rebuilds a second graph from nothing but the episode log those switches
produced, and asserts the two graphs' Hebbian mesh agrees — the whole point of
`CoactivationWindow` being shared code (core/coactivation.py), not two
independent reimplementations of "fire together, wire together".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.episodic_writer import EpisodicWriter
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.graph_rebuild import rebuild_graph, rebuild_graph_to_file
from neuropaca.core.models import Event
from neuropaca.diagnosis import correlator as correlator_mod
from neuropaca.diagnosis.correlator import SignalCorrelator

_BASE = datetime(2026, 9, 1, tzinfo=UTC)


class _BootClock:
    """Stands in for `correlator._now` (`CLOCK_BOOTTIME`) — advanced in
    lockstep with the `FakeClock` driving `EpisodicWriter`, so both systems
    see identical elapsed seconds between switches."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _weights(graph: GraphMemory) -> dict[frozenset[str], float]:
    out: dict[frozenset[str], float] = {}
    for node_id in graph.node_ids:
        if not node_id.startswith("app:"):
            continue
        for edge in graph.get_edges(node_id):
            if edge.relation is not RelationType.RELATED_TO or edge.weight <= 0.0:
                continue
            out[frozenset((edge.source_id, edge.target_id))] = edge.weight
    return out


async def test_rebuild_reproduces_the_live_hebbian_mesh(tmp_path, monkeypatch) -> None:
    boot_clock = _BootClock()
    monkeypatch.setattr(correlator_mod, "_now", boot_clock)

    cfg = Config(inference_backend="fake")
    bus = EventBus.get_instance()
    await bus.start()

    live_graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "live.json"))
    await live_graph.load()
    correlator = SignalCorrelator(bus, cfg, live_graph)
    await correlator.initialize()
    await correlator.start()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    wall_clock = FakeClock(wall=_BASE)
    writer = EpisodicWriter(bus, cfg, store, clock=wall_clock)
    await writer.initialize()
    await writer.start()

    # (app_id, elapsed_seconds_since_previous_switch)
    switches = [("code", 0.0), ("terminal", 60.0), ("code", 30.0), ("notes", 500.0), ("code", 5.0)]
    for app_id, gap in switches:
        if gap:
            boot_clock.t += gap
            await wall_clock.advance(gap)
        event = Event(event_type=EventType.APP_SWITCH, payload={"app_id": app_id, "webapp": None})
        await correlator.on_app_switch(event)
        await writer.on_app_switch(event)
    await store.flush()

    rebuilt, stats = await rebuild_graph(store, cfg, target_path=str(tmp_path / "rebuilt.json"))

    assert stats.focus_spans == 4  # the last switch ("code") never closes; 5 switches -> 4 spans
    live_weights = _weights(live_graph)
    rebuilt_weights = _weights(rebuilt)

    # every pair the rebuild wired must match the live weight closely
    assert rebuilt_weights
    for pair, weight in rebuilt_weights.items():
        assert pair in live_weights, f"rebuild wired {pair} but the live graph never did"
        assert weight == pytest.approx(live_weights[pair], abs=1e-9)

    await correlator.stop()
    await writer.stop()
    await store.stop()
    await bus.stop()


async def test_rebuild_reproduces_sightings(tmp_path) -> None:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    start = _BASE
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", start, start + timedelta(minutes=5))
    await store.flush()

    _rebuilt, stats = await rebuild_graph(
        store, Config(inference_backend="fake"), target_path=str(tmp_path / "g.json")
    )
    assert stats.focus_spans == 1
    assert stats.nodes >= 12  # 11 hubs + app:code
    await store.stop()


async def test_rebuild_decays_across_an_idle_gap(tmp_path) -> None:
    """Two focus spans separated by a long idle gap should decay the mesh
    between them by exactly the elapsed-time factor, not treat the gap as
    instantaneous."""
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    t0 = _BASE
    # code <-> terminal wired once
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", t0, t0 + timedelta(seconds=30))
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:terminal",
        t0 + timedelta(seconds=30),
        t0 + timedelta(minutes=1),
    )
    # a long idle gap, then code focused again alone (no new coactivation partner)
    idle_start = t0 + timedelta(minutes=1)
    idle_end = idle_start + timedelta(hours=200)  # well over the default 72h half-life
    store.record_span(EpisodeKind.IDLE_SPAN, "YOU", idle_start, idle_end)
    store.record_span(
        EpisodeKind.FOCUS_SPAN, "app:code", idle_end, idle_end + timedelta(seconds=30)
    )
    await store.flush()

    cfg = Config(inference_backend="fake")
    rebuilt, _stats = await rebuild_graph(store, cfg, target_path=str(tmp_path / "g.json"))

    weight = _weights(rebuilt).get(frozenset({"app:code", "app:terminal"}))
    assert weight is not None
    # started at hebbian_delta, decayed across ~200h (>2 half-lives of 72h)
    assert weight < cfg.hebbian_delta * 0.3
    await store.stop()


async def test_rebuild_to_file_writes_an_atomic_graph(tmp_path) -> None:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    start = _BASE
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", start, start + timedelta(minutes=5))
    await store.flush()

    target = tmp_path / "graph.json"
    stats = await rebuild_graph_to_file(
        store, Config(inference_backend="fake"), target_path=str(target)
    )
    assert target.exists()
    assert stats.nodes >= 12
    await store.stop()


async def test_empty_log_rebuilds_to_just_the_hubs(tmp_path) -> None:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    rebuilt, stats = await rebuild_graph(
        store, Config(inference_backend="fake"), target_path=str(tmp_path / "g.json")
    )
    assert stats.episodes_replayed == 0
    assert rebuilt.node_count == 11  # YOU + 10 domain hubs, nothing else
    await store.stop()


async def test_node_type_for_webapp_subject(tmp_path) -> None:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    start = _BASE
    store.record_span(EpisodeKind.FOCUS_SPAN, "webapp:gmail", start, start + timedelta(minutes=5))
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "webapp:github",
        start + timedelta(minutes=5),
        start + timedelta(minutes=10),
    )
    await store.flush()

    rebuilt, _stats = await rebuild_graph(
        store, Config(inference_backend="fake"), target_path=str(tmp_path / "g.json")
    )
    node = rebuilt.get_node("webapp:gmail")
    assert node is not None and node.node_type is NodeType.WEBAPP
    await store.stop()


async def test_rebuild_wires_domain_and_browser_structure(tmp_path) -> None:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    start = _BASE
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:code",
        start,
        start + timedelta(minutes=5),
        obj="domain:engineering",
    )
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "webapp:gmail",
        start + timedelta(minutes=5),
        start + timedelta(minutes=10),
        obj="domain:comms",
        attrs={"browser": "app:brave"},
    )
    await store.flush()

    rebuilt, _stats = await rebuild_graph(
        store, Config(inference_backend="fake"), target_path=str(tmp_path / "g.json")
    )

    code_edges = {(e.target_id, e.relation) for e in rebuilt.get_edges("app:code")}
    assert ("domain:engineering", RelationType.PART_OF) in code_edges

    gmail_edges = {(e.target_id, e.relation) for e in rebuilt.get_edges("webapp:gmail")}
    assert ("app:brave", RelationType.PART_OF) in gmail_edges
    assert ("domain:comms", RelationType.PART_OF) in gmail_edges
    assert rebuilt.get_node("app:brave") is not None
    await store.stop()


async def test_rebuild_wires_structure_only_once_per_subject(tmp_path) -> None:
    """A revisited app must not get a second PART_OF edge (which would reset
    any weight on it) — mirrors `SignalCorrelator`'s `_known_apps` gate."""
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    start = _BASE
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:code",
        start,
        start + timedelta(minutes=1),
        obj="domain:engineering",
    )
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:terminal",
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
    )
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:code",
        start + timedelta(minutes=2),
        start + timedelta(minutes=3),
        obj="domain:engineering",
    )
    await store.flush()

    rebuilt, _stats = await rebuild_graph(
        store, Config(inference_backend="fake"), target_path=str(tmp_path / "g.json")
    )
    part_of = [
        e
        for e in rebuilt.get_edges("app:code")
        if e.relation is RelationType.PART_OF and e.target_id == "domain:engineering"
    ]
    assert len(part_of) == 1
    await store.stop()


# gen-ref: 2f6a9c14
