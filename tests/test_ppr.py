# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `GraphMemory.personalized_pagerank` — Forward Push PPR (VISION_PHASES.md §3.4).

Checked against a plain power-iteration reference on the same small graph, not
against a networkx builtin — `_ppr_neighbours_unsafe`'s undirected, hub-damped,
weight-floored neighbourhood is this codebase's own definition of the
transition matrix, so the reference must use exactly that definition too.
"""

from __future__ import annotations

import time

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.graph_memory import GraphMemory


async def _graph(tmp_path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


def _power_iteration_reference(
    gm: GraphMemory, seeds: dict[str, float], *, alpha: float, iterations: int = 200
) -> dict[str, float]:
    """Global power iteration over the exact same neighbourhood function Forward
    Push uses (`_ppr_neighbours_unsafe`), for cross-checking the approximation."""
    nodes = gm.node_ids
    total = sum(seeds.values())
    e = {n: seeds.get(n, 0.0) / total for n in nodes}
    pi = dict(e)
    for _ in range(iterations):
        nxt = {n: alpha * e[n] for n in nodes}
        for v in nodes:
            neighbours = gm._ppr_neighbours_unsafe(v)
            weight_total = sum(neighbours.values())
            if weight_total <= 0.0:
                continue
            for u, w in neighbours.items():
                if u in nxt:
                    nxt[u] += (1.0 - alpha) * pi[v] * (w / weight_total)
        pi = nxt
    return pi


async def test_forward_push_matches_power_iteration_within_eps(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for name in ("code", "terminal", "notes", "browser"):
        await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
    await gm.add_edge("app:code", "app:terminal", RelationType.RELATED_TO, weight=0.8)
    await gm.add_edge("app:terminal", "app:code", RelationType.RELATED_TO, weight=0.8)
    await gm.add_edge("app:terminal", "app:notes", RelationType.RELATED_TO, weight=0.6)
    await gm.add_edge("app:notes", "app:terminal", RelationType.RELATED_TO, weight=0.6)
    await gm.add_edge("app:browser", "app:code", RelationType.RELATED_TO, weight=0.1)
    await gm.add_edge("app:code", "app:browser", RelationType.RELATED_TO, weight=0.1)

    seeds = {"app:code": 1.0}
    eps = 1e-4
    approx = gm.personalized_pagerank(seeds, eps=eps, alpha=0.15)
    reference = _power_iteration_reference(gm, seeds, alpha=0.15)

    for node_id, ref_val in reference.items():
        got = approx.get(node_id, 0.0)
        assert abs(got - ref_val) < 0.02, (node_id, got, ref_val)


async def test_ppr_converges_to_roughly_one(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for name in ("a", "b", "c"):
        await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
    await gm.add_edge("app:a", "app:b", RelationType.RELATED_TO, weight=0.5)
    await gm.add_edge("app:b", "app:c", RelationType.RELATED_TO, weight=0.5)

    result = gm.personalized_pagerank({"app:a": 1.0}, eps=1e-5, alpha=0.15)
    assert 0.8 <= sum(result.values()) <= 1.05


async def test_seeds_matter(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for name in ("a", "b", "c", "d"):
        await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
    await gm.add_edge("app:a", "app:b", RelationType.RELATED_TO, weight=0.9)
    await gm.add_edge("app:b", "app:a", RelationType.RELATED_TO, weight=0.9)
    await gm.add_edge("app:c", "app:d", RelationType.RELATED_TO, weight=0.9)
    await gm.add_edge("app:d", "app:c", RelationType.RELATED_TO, weight=0.9)

    from_a = gm.personalized_pagerank({"app:a": 1.0}, eps=1e-5, alpha=0.15)
    from_c = gm.personalized_pagerank({"app:c": 1.0}, eps=1e-5, alpha=0.15)

    assert from_a.get("app:b", 0.0) > from_a.get("app:d", 0.0)
    assert from_c.get("app:d", 0.0) > from_c.get("app:b", 0.0)


async def test_hub_is_damped_not_dominant(tmp_path) -> None:
    """Every non-hub node structurally reaches `YOU` — undamped, a walk from
    any seed would pool mass there instead of on the seed's real neighbours."""
    gm = await _graph(tmp_path)
    for name in ("code", "terminal"):
        await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
        await gm.add_edge(f"app:{name}", "YOU", RelationType.RELATED_TO, weight=0.0)
    await gm.add_edge("app:code", "app:terminal", RelationType.RELATED_TO, weight=0.9)
    await gm.add_edge("app:terminal", "app:code", RelationType.RELATED_TO, weight=0.9)

    result = gm.personalized_pagerank({"app:code": 1.0}, eps=1e-5, alpha=0.15)
    assert result.get("app:terminal", 0.0) > result.get("YOU", 0.0)


async def test_cost_independent_of_graph_size_at_fixed_local_density(tmp_path) -> None:
    """A big graph with the seed's own neighbourhood unchanged costs about the
    same as a small one — the O(1/(eps*alpha)) bound, not O(graph size)."""
    gm = await _graph(tmp_path)
    for name in ("code", "terminal", "notes"):
        await gm.add_node(f"app:{name}", NodeType.APP, {"label": name})
    await gm.add_edge("app:code", "app:terminal", RelationType.RELATED_TO, weight=0.8)
    await gm.add_edge("app:terminal", "app:code", RelationType.RELATED_TO, weight=0.8)
    await gm.add_edge("app:terminal", "app:notes", RelationType.RELATED_TO, weight=0.6)
    await gm.add_edge("app:notes", "app:terminal", RelationType.RELATED_TO, weight=0.6)

    # A disconnected cluster of far-away nodes the seed's walk never reaches.
    for i in range(2000):
        await gm.add_node(f"app:far{i}", NodeType.APP, {"label": f"far{i}"})

    start = time.perf_counter()
    result = gm.personalized_pagerank({"app:code": 1.0}, eps=1e-4, alpha=0.15)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.005, f"PPR at 2000+ disconnected nodes took {elapsed * 1000:.2f} ms"
    assert all(not n.startswith("app:far") for n in result)


# gen-ref: c9f5a3e1
