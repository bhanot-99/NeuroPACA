# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B4 integration · Hebbian graph math + lock safety at scale (D-11), T7.

Loads the deterministic 10 000-node fixture, wires a real `BitNetPlasticity` +
`GraphMemory`, and drives the co-occurrence path. Proves:

- `reinforce_cooccurrence` adds `+delta` to every **existing** edge between the
  episode's nodes (both directions, all parallel relations) and creates nothing.
- `wire_cooccurrence` (T7) *creates* the peer `RELATED_TO` edge the correlator
  never builds when both ends are activity nodes, then strengthens it — the
  earlier version of this file pre-wired those edges by hand, hiding the fact
  that production never did.
- `_store_insight` citing 50 nodes runs the whole co-occurrence update in **one
  `_lock` cycle** and never wakes a 10 ms loop probe more than 50 ms late.

Excluded from the default suite (`-m 'not integration'`).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.insight import Insight
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.sensing.snapshot import MetricSnapshot

pytestmark = pytest.mark.integration

_LAG_LIMIT_MS = 50.0
_DELTA = 0.01
_SNAP = MetricSnapshot(collector_name="system", timestamp=datetime(2026, 1, 1, tzinfo=UTC), data={})


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + 0.01
        await asyncio.sleep(0.01)
        samples.append((loop.time() - expected) * 1000.0)


async def _loaded_10k(tmp_path: Path) -> GraphMemory:
    from tests.fixtures.generate_10k_graph import FIXTURE_PATH, write_fixture

    if not FIXTURE_PATH.exists():
        write_fixture(FIXTURE_PATH)
    graph = GraphMemory.get_instance(persistence_path=str(FIXTURE_PATH))
    await graph.load()
    graph._path = tmp_path / "graph.json"  # never write the fixture
    return graph


async def test_reinforce_cooccurrence_bumps_only_existing_edges(tmp_path: Path) -> None:
    graph = await _loaded_10k(tmp_path)
    # fresh ids so there are zero pre-existing edges among the 50
    cited = [f"hebbian:{i:02d}" for i in range(50)]
    for nid in cited:
        await graph.upsert_node(nid, NodeType.CONCEPT, {"label": nid})

    linked_pairs = [(cited[i], cited[i + 1]) for i in range(0, 40, 2)]  # 20 pairs
    for a, b in linked_pairs:
        await graph.add_edge(a, b, RelationType.RELATED_TO, weight=0.5)
    baseline_edges = graph.edge_count

    bumped = await graph.reinforce_cooccurrence(cited, _DELTA)

    assert bumped == len(linked_pairs), f"expected {len(linked_pairs)} bumps, got {bumped}"
    assert graph.edge_count == baseline_edges, "reinforce_cooccurrence created an edge"
    for a, b in linked_pairs:
        edge = next(e for e in graph.get_edges(a) if e.target_id == b)
        assert edge.weight == pytest.approx(0.5 + _DELTA)
    # an unlinked pair among the 50 still has no edge
    assert not any(e.target_id == cited[49] for e in graph.get_edges(cited[0]))


async def test_wire_cooccurrence_creates_the_mesh_the_correlator_never_builds(
    tmp_path: Path,
) -> None:
    """T7 · the production gap: activity nodes are wired to `domain:` hubs, never
    to each other, so `reinforce_cooccurrence` had nothing to bump. `wire_cooccurrence`
    creates the peer edge on first co-activation and strengthens it after."""
    graph = await _loaded_10k(tmp_path)
    apps = [f"app:t7-{i}" for i in range(4)]
    for nid in apps:
        await graph.upsert_node(nid, NodeType.APP, {"label": nid})
    baseline_edges = graph.edge_count

    created, bumped = await graph.wire_cooccurrence(apps, delta=_DELTA)
    assert (created, bumped) == (6, 0)  # C(4,2) new RELATED_TO edges
    assert graph.edge_count == baseline_edges + 6
    for i, a in enumerate(apps):
        for b in apps[i + 1 :]:
            assert next(e for e in graph.get_edges(a) if e.target_id == b).weight == pytest.approx(
                _DELTA  # one saturating step from 0
            )

    created2, bumped2 = await graph.wire_cooccurrence(apps, delta=_DELTA)
    assert (created2, bumped2) == (0, 6)  # second pass only strengthens
    assert graph.edge_count == baseline_edges + 6
    a, b = apps[0], apps[1]
    assert next(e for e in graph.get_edges(a) if e.target_id == b).weight == pytest.approx(
        _DELTA + _DELTA * (1 - _DELTA)
    )

    # a non-activity node in the episode is never a *new* edge's endpoint
    await graph.upsert_node("concept:t7", NodeType.CONCEPT, {"label": "c"})
    created3, _ = await graph.wire_cooccurrence([apps[0], "concept:t7"], delta=_DELTA)
    assert created3 == 0


async def test_store_insight_with_50_citations_stays_off_the_loop(tmp_path: Path) -> None:
    graph = await _loaded_10k(tmp_path)
    bus = EventBus.get_instance()
    await bus.start()
    module = BitNetPlasticity(
        bus, Config(inference_backend="fake"), graph, BitNetRuntime.get_instance()
    )

    # `leaf:` nodes are not activity nodes, so wire_cooccurrence only strengthens
    # the pre-existing edges here — it creates none — keeping the edge-count math
    # simple while still exercising the O(k^2) one-lock update.
    cited = tuple(f"leaf:{i:05d}" for i in range(50))
    for i in range(0, 48, 2):
        await graph.add_edge(cited[i], cited[i + 1], RelationType.RELATED_TO, weight=0.1)
    edges_before = graph.edge_count
    insight_delta = module.config.hebbian_delta * module.config.hebbian_insight_multiplier

    insight = Insight(
        category="anomaly",
        cited_node_ids=cited,
        source_signal=SignalType.HIGH_LOAD,
        confidence=0.9,
        snapshot_count=1,
    )
    signal = Signal(
        signal_type=SignalType.HIGH_LOAD,
        confidence=0.9,
        related_node_ids=cited,
        source_snapshots=(_SNAP,),
        reason="integration",
    )

    stop = asyncio.Event()
    lag: list[float] = []
    probe = asyncio.create_task(_probe(lag, stop))
    await asyncio.sleep(0.1)

    t0 = time.perf_counter()
    stored, _created = await module._store_insight(insight, signal)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    await asyncio.sleep(0.05)

    stop.set()
    probe.cancel()
    try:
        await probe
    except asyncio.CancelledError:
        pass

    # the INSIGHT node + its 50 RELATED_TO edges are the only new edges
    node = graph.get_node(stored.node_id)
    assert node is not None and node.node_type is NodeType.INSIGHT
    assert graph.edge_count == edges_before + len(cited)

    # the 24 pre-existing co-occurrence edges each gained exactly one insight bump
    for i in range(0, 48, 2):
        edge = next(e for e in graph.get_edges(cited[i]) if e.target_id == cited[i + 1])
        assert edge.weight == pytest.approx(0.1 + insight_delta)

    assert lag, "probe never sampled"
    assert max(lag) < _LAG_LIMIT_MS, f"_store_insight stalled the loop {max(lag):.1f} ms"
    print(f"\n_store_insight(50 citations) wall {wall_ms:.1f} ms · max loop lag {max(lag):.1f} ms")

    await bus.stop()


# gen-ref: 45ee7ddd
