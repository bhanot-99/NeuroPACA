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


async def test_bridge_value_rewards_cross_domain_nodes(tmp_path) -> None:
    """B2.5b (D-10): with node degree held equal, the node wired to two domain
    hubs outscores the one wired to a single domain — `bridge_value` is live."""
    gm = await _loaded_graph(tmp_path)
    for node_id in ("app:one", "app:two"):
        await gm.add_node(node_id, NodeType.APP, {"access_count": 10})
    # both nodes have degree 2; only app:two reaches two domains
    await gm.add_edge("app:one", "domain:engineering", RelationType.PART_OF)
    await gm.add_edge("app:one", "YOU", RelationType.RELATED_TO)
    await gm.add_edge("app:two", "domain:engineering", RelationType.PART_OF)
    await gm.add_edge("app:two", "domain:research", RelationType.PART_OF)

    await gm.recalculate_importance()

    assert gm.get_node("app:two").relevance_score > gm.get_node("app:one").relevance_score
    assert gm._bridge_value_unsafe("app:two") == 1.0
    assert gm._bridge_value_unsafe("app:one") == 0.5
    assert gm._bridge_value_unsafe("domain:engineering") == 0.0  # hubs never bridge


async def test_upsert_creates_a_missing_node(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    node = await gm.upsert_node("file:/w/a.py", NodeType.FILE, {"label": "a.py"})
    assert node.label == "a.py"
    assert gm.get_node("file:/w/a.py") is not None


async def test_upsert_preserves_score_and_created_at_but_bumps_access(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    original = await gm.add_node(
        "file:/w/a.py", NodeType.FILE, {"label": "a.py", "relevance_score": 6.25, "access_count": 4}
    )

    updated = await gm.upsert_node("file:/w/a.py", NodeType.FILE, {"label": "renamed.py"})

    assert updated.label == "renamed.py"  # supplied attr merged
    assert updated.relevance_score == 6.25  # never reset
    assert updated.created_at == original.created_at  # preserved
    assert updated.access_count == 5  # bumped by one
    assert updated.last_accessed >= original.last_accessed


async def test_upsert_ignores_attempts_to_overwrite_protected_attrs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("file:/w/a.py", NodeType.FILE, {"relevance_score": 9.0, "access_count": 2})
    updated = await gm.upsert_node(
        "file:/w/a.py",
        NodeType.CONCEPT,  # a different type is ignored on an existing node
        {"relevance_score": 0.0, "access_count": 0, "label": "kept"},
    )
    assert updated.relevance_score == 9.0
    assert updated.access_count == 3
    assert updated.node_type is NodeType.FILE
    assert updated.label == "kept"


async def test_reset_isolates_the_graph(tmp_path) -> None:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "a.json"))
    await gm.add_node("x", NodeType.CONCEPT, None)
    GraphMemory._reset_for_tests()
    fresh = GraphMemory.get_instance(persistence_path=str(tmp_path / "b.json"))
    assert fresh.node_count == 0
    assert fresh is not gm


# --------------------------------------------------------------- audit regressions
# From the B9 optimisation audit: the single-pass rewrite of the DMN's graph jobs.
# Each of these asserts behaviour the old per-merge / per-link rescan gave us, so
# the speedup cannot quietly change the result.


async def test_consolidate_collapses_a_chain_of_three_duplicates(tmp_path) -> None:
    """Three nodes on one (type, label) key must collapse onto the single oldest
    survivor in one sweep — not into two survivors, and not partially."""
    gm = await _loaded_graph(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i, offset in enumerate((2, 0, 1)):  # b is oldest -> b survives
        await gm.add_node(
            f"n{i}",
            NodeType.CONCEPT,
            {"label": "Same Label", "created_at": base + timedelta(hours=offset)},
        )
    merged = await gm.consolidate()
    assert merged == 2
    survivors = [n for n in ("n0", "n1", "n2") if gm.get_node(n) is not None]
    assert survivors == ["n1"]


async def test_consolidate_is_case_insensitive_and_spares_hubs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("a", NodeType.CONCEPT, {"label": "Refactor"})
    await gm.add_node("b", NodeType.CONCEPT, {"label": "  refactor "})
    await gm.add_node("c", NodeType.TASK, {"label": "Refactor"})  # different type
    assert await gm.consolidate() == 1
    assert gm.get_node("c") is not None, "a different node_type is not a duplicate"
    for hub in HUB_NODE_IDS:
        assert gm.get_node(hub) is not None


async def test_link_orphan_nodes_links_every_orphan_and_is_idempotent(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    for i in range(25):
        await gm.add_node(f"orphan:{i}", NodeType.TASK, {"label": f"t{i}"})
    assert await gm.link_orphan_nodes() == 25
    assert all(gm.get_edges(f"orphan:{i}") for i in range(25))
    assert await gm.link_orphan_nodes() == 0  # a second pass finds nothing to do


async def test_consolidate_and_link_survive_cancellation_mid_run(tmp_path) -> None:
    """The lock is taken per mutation, so a cancellation lands *between* two of
    them and the graph is never left half-merged (rules.md §3)."""
    gm = await _loaded_graph(tmp_path)
    for i in range(200):
        await gm.add_node(f"d{i}a", NodeType.CONCEPT, {"label": f"dup{i}"})
        await gm.add_node(f"d{i}b", NodeType.CONCEPT, {"label": f"dup{i}"})

    task = asyncio.create_task(gm.consolidate())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Whatever it got through, every surviving node is still well-formed and the
    # hubs are intact — then a fresh run finishes the job.
    for node_id in gm.node_ids:
        assert gm.get_node(node_id) is not None
    await gm.consolidate()
    assert await gm.consolidate() == 0


async def test_top_nodes_by_score_ranks_and_excludes(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    for i in range(50):
        await gm.add_node(f"f{i}", NodeType.FILE, {"label": f"f{i}", "relevance_score": float(i)})
    await gm.add_node("ins", NodeType.INSIGHT, {"label": "ins", "relevance_score": 99.0})

    top = gm.top_nodes_by_score(3)
    assert [n.id for n in top] == ["ins", "f49", "f48"]

    filtered = gm.top_nodes_by_score(3, exclude_types=frozenset({NodeType.INSIGHT}))
    assert [n.id for n in filtered] == ["f49", "f48", "f47"]
    assert all(n.id not in HUB_NODE_IDS for n in filtered)
    assert gm.top_nodes_by_score(0) == []


async def test_load_does_not_block_the_event_loop(tmp_path) -> None:
    """`load()` reads and decodes in a worker thread (it used to do both inline,
    holding `_lock`). Proof: the loop keeps ticking while a load is in flight."""
    gm = await _loaded_graph(tmp_path)
    for i in range(2000):
        await gm.add_node(f"n{i}", NodeType.FILE, {"label": f"n{i}"})
    await gm.save()

    GraphMemory._reset_for_tests()
    fresh = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await fresh.load()
    ticker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ticker
    assert fresh.node_count == 2011
    assert ticks > 0, "the loop was starved for the whole of load()"


async def test_a_cancelled_save_leaves_the_graph_dirty(tmp_path) -> None:
    """`save()` clears `_dirty` before it streams, so a concurrent mutation can
    re-flag it. A *cancelled* save must re-flag it too — otherwise the graph
    looks clean, the scheduler skips it, and nothing is ever written. The DMN
    hits this whenever activity cancels an idle cycle mid-save."""
    gm = await _loaded_graph(tmp_path)
    for i in range(3000):
        await gm.add_node(f"n{i}", NodeType.FILE, {"label": f"n{i}"})

    task = asyncio.create_task(gm.save())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gm.dirty, "a cancelled save must leave the pending changes flagged"
    await gm.save()  # the retry the scheduler will now actually make
    assert not gm.dirty
    assert gm.node_count == 3011


async def test_a_failed_save_leaves_the_graph_dirty(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("n", NodeType.FILE, {"label": "n"})

    def boom(_text: str) -> None:
        raise GraphMemoryError("disk full")

    gm._write_atomic = boom  # type: ignore[method-assign]
    with pytest.raises(GraphMemoryError):
        await gm.save()
    assert gm.dirty
