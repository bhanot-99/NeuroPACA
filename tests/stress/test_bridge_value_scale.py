"""Stress · bridge_value math must not break the T4 chunked-yield model (D-10).

`GraphMemory._recalculate_chunk_unsafe` now computes a real `bridge_value` per
node (`_bridge_value_unsafe` — the node's distinct `domain:*` reach). T4
established that whole-graph recalculation runs in bounded chunks with an
`await asyncio.sleep(0)` between them so the event loop is never starved
(problems.md T4, rules.md §3).

This loads the 10k-node fixture, injects 15 000 extra `domain:*` edges across the
leaves (dense behavioural classification), then runs `recalculate_importance()`
with a 10 ms loop-lag probe alongside. The new per-node set math must not push
any single chunk past the 50 ms budget, and the scores it produces must actually
reflect cross-domain reach.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from neuropaca.core.enums import RelationType  # noqa: E402
from neuropaca.core.graph_memory import DOMAIN_HUB_IDS, GraphMemory  # noqa: E402

pytestmark = pytest.mark.stress

_INJECTED_EDGES = 15_000
_LAG_LIMIT_MS = 50.0
_PROBE_INTERVAL_S = 0.01
_SEED = 20260831


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + _PROBE_INTERVAL_S
        await asyncio.sleep(_PROBE_INTERVAL_S)
        samples.append((loop.time() - expected) * 1000.0)


async def test_bridge_value_recalc_stays_under_the_50ms_loop_budget(tmp_path: Path) -> None:
    from tests.fixtures.generate_10k_graph import FIXTURE_PATH, write_fixture

    if not FIXTURE_PATH.exists():
        write_fixture(FIXTURE_PATH)

    graph = GraphMemory.get_instance(persistence_path=str(FIXTURE_PATH))
    await graph.load()
    graph._path = tmp_path / "graph.json"  # never write the fixture
    assert graph.node_count == 10_011

    # --- inject dense domain:* classification across the leaves ----------------
    rng = random.Random(_SEED)
    hubs = sorted(DOMAIN_HUB_IDS)
    leaves = [n for n in graph.node_ids if n.startswith("leaf:")]
    probe_leaf = rng.choice(leaves)
    await graph.add_edge(probe_leaf, hubs[0], RelationType.PART_OF)
    await graph.add_edge(probe_leaf, hubs[1], RelationType.PART_OF)  # -> two domains
    for _ in range(_INJECTED_EDGES):
        await graph.add_edge(rng.choice(leaves), rng.choice(hubs), RelationType.PART_OF)

    assert graph._bridge_value_unsafe(probe_leaf) == 1.0

    # --- recalc under a loop-lag probe ---------------------------------------
    stop = asyncio.Event()
    lag: list[float] = []
    probe = asyncio.create_task(_probe(lag, stop))
    await asyncio.sleep(0.2)  # let the probe settle

    mark = len(lag)
    t0 = time.perf_counter()
    await graph.recalculate_importance()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    await asyncio.sleep(0.05)  # let the tail of the probe land

    stop.set()
    probe.cancel()
    try:
        await probe
    except asyncio.CancelledError:
        pass

    during = lag[mark:] or [0.0]
    peak = max(during)
    print(f"\nrecalc wall {wall_ms:.0f} ms · max loop lag during recalc {peak:.1f} ms")

    assert peak < _LAG_LIMIT_MS, (
        f"bridge_value recalc stalled the loop {peak:.1f} ms (>= {_LAG_LIMIT_MS:.0f}) — "
        "the chunked asyncio.sleep(0) yield is broken"
    )

    # the scores really moved with cross-domain reach
    scored = graph.get_node(probe_leaf)
    assert scored is not None and scored.relevance_score > 0.0
    for hub_id in DOMAIN_HUB_IDS:
        assert graph._bridge_value_unsafe(hub_id) == 0.0  # hubs never bridge
