"""B1 · GraphMemory — concurrency, traversal limits, atomic persistence, protected
pruning (Architecture.md §3.2, D-5, problems.md 1.10).

Skips until `core/graph_memory.py` exists; it lands with these tests in one
commit (rules.md §8).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("neuropaca.core.graph_memory")

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.graph_memory import HUB_NODE_IDS, GraphMemory


async def _loaded_graph(tmp_path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()  # seeds YOU + the 10 domain hubs when empty
    return gm


async def test_load_seeds_exactly_the_eleven_hubs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    assert gm.node_count == 11
    for hub_id in HUB_NODE_IDS:
        assert gm.get_node(hub_id) is not None
    assert "YOU" in HUB_NODE_IDS
    assert "domain:engineering" in HUB_NODE_IDS


async def test_concurrent_writers_serialise_without_corruption(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)

    await asyncio.gather(
        *(gm.add_node(f"n{i}", NodeType.CONCEPT, {"label": f"node {i}"}) for i in range(100))
    )
    assert gm.node_count == 111
    assert all(gm.get_node(f"n{i}") is not None for i in range(100))

    # 100 concurrent updates to the SAME node must not deadlock or raise; the
    # lock makes each mutation atomic, so the last writer wins cleanly.
    await asyncio.gather(*(gm.update_node("n0", {"relevance_score": float(i)}) for i in range(100)))
    score = gm.get_node("n0").relevance_score
    assert 0.0 <= score <= 99.0


async def test_concurrent_edge_writes_land_all(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await asyncio.gather(*(gm.add_node(f"n{i}", NodeType.FILE, None) for i in range(20)))

    await asyncio.gather(
        *(gm.add_edge(f"n{i}", f"n{(i + 1) % 20}", RelationType.RELATED_TO) for i in range(20))
    )
    assert gm.edge_count == 20


async def test_parallel_relations_between_the_same_pair_coexist(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("a", NodeType.TASK, None)
    await gm.add_node("b", NodeType.TASK, None)

    await gm.add_edge("a", "b", RelationType.CAUSED_BY)
    await gm.add_edge("a", "b", RelationType.FOLLOWED_BY)

    assert gm.edge_count == 2  # MultiDiGraph keeps both (D-5)


async def test_find_related_does_not_traverse_through_hubs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    for name in ("n1", "n2", "n3"):
        await gm.add_node(name, NodeType.CONCEPT, None)

    # n1 and n2 are only reachable from each other *through* YOU.
    await gm.add_edge("n1", "YOU", RelationType.PART_OF)
    await gm.add_edge("YOU", "n2", RelationType.PART_OF)
    # n1 and n3 are only reachable through a domain hub.
    await gm.add_edge("n1", "domain:engineering", RelationType.PART_OF)
    await gm.add_edge("domain:engineering", "n3", RelationType.PART_OF)

    related_ids = {n.id for n in gm.find_related("n1", depth=2)}
    assert "n2" not in related_ids
    assert "n3" not in related_ids

    # Opt in and the hub becomes a through-route again.
    related_open = {n.id for n in gm.find_related("n1", depth=2, traverse_hubs=True)}
    assert {"n2", "n3"} <= related_open


async def test_save_is_atomic_when_replace_crashes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "graph.json"
    gm = GraphMemory.get_instance(persistence_path=str(path))
    await gm.load()
    await gm.add_node("keeper", NodeType.CONCEPT, {"label": "original"})
    await gm.save()
    good_bytes = path.read_bytes()

    await gm.add_node("doomed", NodeType.CONCEPT, {"label": "should not persist"})

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated crash during os.replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(GraphMemoryError):
        await gm.save()

    assert path.read_bytes() == good_bytes  # untouched
    assert list(tmp_path.glob("*.tmp")) == []  # temp file cleaned up

    monkeypatch.undo()
    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(path))
    await reloaded.load()
    assert reloaded.get_node("keeper") is not None
    assert reloaded.get_node("doomed") is None


async def test_prune_removes_low_score_nodes_but_never_hubs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    stale = datetime.now(UTC) - timedelta(days=30)

    await gm.add_node("junk", NodeType.CONCEPT, {"relevance_score": 1.0})
    await gm.update_node("junk", {"last_accessed": stale})
    # Force the hubs to look prunable too — low score, ancient.
    for hub_id in HUB_NODE_IDS:
        await gm.update_node(hub_id, {"relevance_score": 0.0, "last_accessed": stale})

    removed = await gm.prune(older_than=timedelta(days=1), min_importance=5.0)

    assert removed == 1
    assert gm.get_node("junk") is None
    for hub_id in HUB_NODE_IDS:
        assert gm.get_node(hub_id) is not None


async def test_recalculate_importance_keeps_scores_in_range(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("hot", NodeType.APP, {"access_count": 500})
    await gm.add_edge("hot", "YOU", RelationType.RELATED_TO)
    await gm.recalculate_importance()
    assert 0.0 <= gm.get_node("hot").relevance_score <= 10.0


async def test_reset_isolates_the_graph(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "a.json"))
    await gm.add_node("x", NodeType.CONCEPT, None)
    GraphMemory._reset_for_tests()
    fresh = GraphMemory.get_instance(persistence_path=str(tmp_path / "b.json"))
    assert fresh.node_count == 0
    assert fresh is not gm
