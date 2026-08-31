"""Stress · GraphMemory lock contention under high read/write I/O (D-5, problems.md 1.10).

The single ``asyncio.Lock`` serialises every mutation; ``find_related`` /
``get_node`` are lock-free synchronous reads that are atomic w.r.t. other
coroutines (single-threaded loop, no ``await`` inside them). This fires 1 000
``upsert_node`` writes against 5 000 ``find_related`` reads at once and proves:

- no deadlock — the compound lock discipline never wedges
- data integrity — every node keeps its full attribute set, protected fields
  (``relevance_score``) untouched, hubs intact
- the whole 6 000-op batch clears well under the time budget, i.e. the lock is
  not a bottleneck at L3-scale write rates
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from neuropaca.core.enums import NodeType
from neuropaca.core.graph_memory import HUB_NODE_IDS, GraphMemory
from tests.fixtures.generate_10k_graph import FIXTURE_PATH, write_fixture

pytestmark = pytest.mark.stress

_UPSERTS = 1_000
_QUERIES = 5_000
_EXISTING_TARGETS = 50  # existing leaves hammered by the even-indexed upserts
_TIME_BUDGET_S = 2.0
_REQUIRED_ATTRS = ("node_type", "label", "created_at", "last_accessed", "access_count")


@pytest.fixture(scope="session")
def graph_10k_path() -> Path:
    if not FIXTURE_PATH.exists():
        write_fixture(FIXTURE_PATH)
    return FIXTURE_PATH


async def test_graph_survives_concurrent_upserts_and_traversals(graph_10k_path: Path) -> None:
    graph = GraphMemory.get_instance(persistence_path=str(graph_10k_path))
    await graph.load()

    nodes_before = graph.node_count
    leaves = [n for n in graph.node_ids if n not in HUB_NODE_IDS]
    assert len(leaves) >= _EXISTING_TARGETS

    targets = leaves[:_EXISTING_TARGETS]
    before = {t: graph.get_node(t) for t in targets}
    assert all(node is not None for node in before.values())

    async def upsert(i: int) -> None:
        if i % 2 == 0:
            target = targets[(i // 2) % _EXISTING_TARGETS]  # existing -> merge path
        else:
            target = f"stress:new:{i}"  # unseen -> create path
        await graph.upsert_node(target, NodeType.FILE, {"label": f"n{i}", "touched": i})

    async def query(i: int) -> int:
        # sync, lock-free read racing the writers above
        return len(graph.find_related(leaves[i % len(leaves)], depth=2))

    start = time.perf_counter()
    await asyncio.gather(
        *(upsert(i) for i in range(_UPSERTS)),
        *(query(i) for i in range(_QUERIES)),
    )
    elapsed = time.perf_counter() - start

    # --- no deadlock: gather returned and the lock is free ---------------------
    assert not graph._lock.locked()
    assert elapsed < _TIME_BUDGET_S, (
        f"{_UPSERTS + _QUERIES} ops took {elapsed:.3f}s "
        f"(budget {_TIME_BUDGET_S}s) — the lock is a bottleneck"
    )

    # --- integrity: no orphaned / missing attributes anywhere -----------------
    for node_id in graph.node_ids:
        data = graph._graph.nodes[node_id]
        for attr in _REQUIRED_ATTRS:
            assert attr in data, f"node {node_id} lost attribute {attr!r}"
        node = graph.get_node(node_id)  # round-trips through the typed model
        assert node is not None and node.label != ""

    # --- every write landed exactly once, protected fields preserved ----------
    bumps_per_target = len(range(0, _UPSERTS, 2)) // _EXISTING_TARGETS
    for target, prior in before.items():
        assert prior is not None
        after = graph.get_node(target)
        assert after is not None
        assert after.access_count == prior.access_count + bumps_per_target
        assert after.relevance_score == prior.relevance_score  # upsert never resets it
        assert after.created_at == prior.created_at

    new_nodes = len(range(1, _UPSERTS, 2))
    assert graph.node_count == nodes_before + new_nodes
    for hub in HUB_NODE_IDS:
        assert graph.get_node(hub) is not None
