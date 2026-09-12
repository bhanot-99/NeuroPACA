# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

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
    """B2.5b (D-10), reworked V-2: one domain is not a bridge (every mapped app
    has one), two domains earn half, three the full bonus. Activity and
    strength are held equal, so the scores order by bridge alone."""
    gm = await _loaded_graph(tmp_path)
    reach = {
        "app:one": ("engineering",),
        "app:two": ("engineering", "research"),
        "app:three": ("engineering", "research", "habits"),
    }
    for node_id, domains in reach.items():
        await gm.add_node(node_id, NodeType.APP, {"access_count": 10})
        for slug in domains:
            await gm.add_edge(node_id, f"domain:{slug}", RelationType.PART_OF)

    await gm.recalculate_importance()

    assert gm._bridge_value_unsafe("app:one") == 0.0
    assert gm._bridge_value_unsafe("app:two") == 0.5
    assert gm._bridge_value_unsafe("app:three") == 1.0
    score = {n: gm.get_node(n).relevance_score for n in reach}
    assert score["app:three"] > score["app:two"] > score["app:one"]
    assert gm._bridge_value_unsafe("domain:engineering") == 0.0  # hubs never bridge


async def test_bridge_counts_domains_reached_through_associations(tmp_path) -> None:
    """V-2 · an app used alongside apps from other domains bridges them, even
    though its own `PART_OF` reaches one domain; a faint association does not
    count."""
    gm = await _loaded_graph(tmp_path)
    for node_id, slug in (
        ("app:notes", "research"),
        ("app:term", "engineering"),
        ("app:music", "habits"),
        ("app:mail", "comms"),
    ):
        await gm.add_node(node_id, NodeType.APP, None)
        await gm.add_edge(node_id, f"domain:{slug}", RelationType.PART_OF)
    await gm.add_edge("app:notes", "app:term", RelationType.RELATED_TO, weight=0.5)
    await gm.add_edge("app:notes", "app:music", RelationType.RELATED_TO, weight=0.05)  # faint
    assert gm._bridge_value_unsafe("app:notes") == 0.5  # research + engineering

    await gm.add_edge("app:mail", "app:notes", RelationType.RELATED_TO, weight=0.3)
    assert gm._bridge_value_unsafe("app:notes") == 1.0  # + comms


async def test_activity_counts_creation_decays_and_adds_one_per_touch(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:a", NodeType.APP, {"label": "a"})
    assert gm.get_node("app:a").activity == pytest.approx(1.0)  # creation = one sighting

    await gm.update_node("app:a", {"last_accessed": datetime.now(UTC) - timedelta(days=7)})
    await gm.upsert_node("app:a", NodeType.APP, {"activity": 99.0})  # protected
    node = gm.get_node("app:a")
    assert node.activity == pytest.approx(1.5, rel=1e-3)  # 1 halved over one half-life, + 1
    assert node.access_count == 1  # the lifetime tally is unchanged in meaning


async def test_score_uses_the_range_and_a_probe_never_outranks_a_used_app(tmp_path) -> None:
    """V-2 · the busiest node reaches the top of the scale, an untouched node
    sits near the bottom (no flat recency floor), and a generated probe's
    provenance edge lends it no strength."""
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, {"access_count": 250})
    await gm.add_node("app:pytest", NodeType.APP, {"access_count": 4})
    await gm.add_node("app:unused", NodeType.APP, None)
    await gm.add_edge("app:unused", "YOU", RelationType.RELATED_TO)  # orphan placeholder
    await gm.add_node("ephemeral:p", NodeType.CONCEPT, None)
    await gm.add_edge("ephemeral:p", "app:pytest", RelationType.RELATED_TO)

    await gm.recalculate_importance()
    score = {n: gm.get_node(n).relevance_score for n in gm.node_ids}

    assert score["app:brave"] >= 6.0  # full activity term
    assert score["app:pytest"] > score["ephemeral:p"]
    assert score["app:unused"] < 1.0
    assert gm._strength_unsafe("ephemeral:p") == 0.0  # provenance edge, not relevance
    assert gm._strength_unsafe("app:pytest") == 0.0  # ...in either direction
    assert gm._strength_unsafe("app:unused") == 0.0  # a YOU edge is not a tie


async def test_a_v5_graph_loads_with_activity_estimated_from_access_count(tmp_path) -> None:
    import json

    path = tmp_path / "graph.json"
    now = datetime.now(UTC).isoformat()
    record = {
        "id": "app:x",
        "node_type": "app",
        "label": "x",
        "created_at": now,
        "last_accessed": now,
        "access_count": 9,
        "relevance_score": 3.0,
        "priority": 0,
    }
    path.write_text(json.dumps({"schema_version": 5, "nodes": [record], "edges": []}))
    gm = GraphMemory.get_instance(persistence_path=str(path))
    await gm.load()
    assert gm.get_node("app:x").activity == pytest.approx(10.0)

    await gm.save()
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 8
    (x,) = (n for n in saved["nodes"] if n["id"] == "app:x")
    assert x["activity"] == pytest.approx(10.0)


async def test_consolidate_sums_activity_as_of_the_later_touch(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    later = datetime.now(UTC)
    earlier = later - timedelta(days=7)
    await gm.add_node(
        "app:zoom", NodeType.APP, {"label": "Zoom", "activity": 4.0, "last_accessed": later}
    )
    await gm.add_node(
        "app:Zoom2", NodeType.APP, {"label": "zoom", "activity": 4.0, "last_accessed": earlier}
    )
    assert await gm.consolidate() == 1
    (survivor,) = (gm.get_node(n) for n in gm.node_ids if n.startswith("app:"))
    assert survivor.activity == pytest.approx(6.0, rel=1e-3)  # 4 + 4 halved
    assert survivor.last_accessed == later


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


# ------------------------------------------------------ B17 · canonicalise apps

_CANON = {
    "brave-browser": "brave",
    "brave": "brave",
    "com.system76.CosmicFiles": "cosmic-files",
    "cosmic-files": "cosmic-files",
}


def _resolve(raw: str) -> str:
    return _CANON.get(raw, raw)


def _is_non_app(raw: str) -> bool:
    return raw in {"MainThread", "Thread-1"}


async def test_canonicalise_folds_the_two_naming_schemes(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    # the focus node: behavioural edges + access
    await gm.add_node("app:brave-browser", NodeType.APP, {"label": "brave-browser"})
    await gm.add_node("webapp:github", NodeType.WEBAPP, {"label": "github"})
    await gm.add_edge("webapp:github", "app:brave-browser", RelationType.PART_OF)
    await gm.upsert_node("app:brave-browser", NodeType.APP, {"label": "brave-browser"})  # ac -> 1
    # the census node: RAM, no edges
    await gm.add_node("app:brave", NodeType.APP, {"label": "brave", "ram_mb": 3811.0})
    # junk the census mistook for a process
    await gm.add_node("app:MainThread", NodeType.APP, {"label": "MainThread"})

    merged, dropped = await gm.canonicalise_app_nodes(_resolve, _is_non_app)
    assert (merged, dropped) == (1, 1)

    assert gm.get_node("app:brave-browser") is None
    assert gm.get_node("app:MainThread") is None
    survivor = gm.get_node("app:brave")
    assert survivor is not None
    assert survivor.ram_mb == 3811.0  # census attr kept
    assert survivor.access_count == 1  # focus count kept
    # the webapp edge followed the merge
    assert any(e.target_id == "app:brave" for e in gm.get_edges("webapp:github"))
    # idempotent
    assert await gm.canonicalise_app_nodes(_resolve, _is_non_app) == (0, 0)


async def test_canonicalise_renames_a_lone_focus_node(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node(
        "app:com.system76.CosmicFiles", NodeType.APP, {"label": "com.system76.CosmicFiles"}
    )
    await gm.add_edge("app:com.system76.CosmicFiles", "domain:tools", RelationType.PART_OF)
    merged, dropped = await gm.canonicalise_app_nodes(_resolve, _is_non_app)
    assert (merged, dropped) == (0, 0)  # nothing to merge or drop — just a rename
    assert gm.get_node("app:com.system76.CosmicFiles") is None
    node = gm.get_node("app:cosmic-files")
    assert node is not None and node.label == "cosmic-files"
    assert any(e.target_id == "domain:tools" for e in gm.get_edges("app:cosmic-files"))


async def test_canonicalise_drops_every_non_app_node_and_its_edges(tmp_path) -> None:
    # `is_non_app` is written to only ever match thread labels / bare shells, so
    # it is trusted — `access_count` is not a "was focused" signal (it bumps on
    # every census upsert), so the drop is unconditional.
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:Thread-1", NodeType.APP, {"label": "Thread-1"})
    await gm.upsert_node("app:Thread-1", NodeType.APP, {"label": "Thread-1"})  # ac -> 1
    await gm.add_edge("app:Thread-1", "domain:system", RelationType.RELATED_TO)
    _merged, dropped = await gm.canonicalise_app_nodes(_resolve, _is_non_app)
    assert dropped == 1
    assert gm.get_node("app:Thread-1") is None
    assert not any(e.source_id == "app:Thread-1" for e in gm.get_edges("domain:system"))


async def test_canonicalise_never_touches_hubs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.canonicalise_app_nodes(_resolve, _is_non_app)
    for hub in HUB_NODE_IDS:
        assert gm.get_node(hub) is not None


# ------------------------------------------------------- T7 · Hebbian plasticity


async def test_add_edge_never_resets_an_existing_edges_weight(tmp_path) -> None:
    """A pattern re-asserting an edge it already owns, or the APP_SWITCH path
    re-classifying after a restart, must not zero the accumulated weight (T7)."""
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:zed", NodeType.APP, {"label": "Zed"})
    await gm.add_edge("app:zed", "domain:engineering", RelationType.PART_OF)
    await gm.reinforce_edge("app:zed", "domain:engineering", 0.35)
    created_at = next(
        e for e in gm.get_edges("app:zed") if e.target_id == "domain:engineering"
    ).created_at

    await gm.add_edge("app:zed", "domain:engineering", RelationType.PART_OF)  # re-assert

    edge = next(e for e in gm.get_edges("app:zed") if e.target_id == "domain:engineering")
    assert edge.weight == pytest.approx(0.35)
    assert edge.created_at == created_at


async def test_wire_cooccurrence_creates_then_strengthens_activity_pairs(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    apps = ["app:zed", "app:brave", "webapp:github"]
    for nid in apps:
        await gm.add_node(nid, NodeType.APP if nid.startswith("app:") else NodeType.WEBAPP, None)

    created, bumped = await gm.wire_cooccurrence(apps, delta=0.1)
    assert (created, bumped) == (3, 0)  # C(3,2)
    for i, a in enumerate(apps):
        for b in apps[i + 1 :]:
            w = next(e for e in gm.get_edges(a) if e.target_id == b).weight
            assert w == pytest.approx(0.1)  # one saturating step from 0

    created2, bumped2 = await gm.wire_cooccurrence(apps, delta=0.1)
    assert (created2, bumped2) == (0, 3)
    assert next(e for e in gm.get_edges("app:zed") if e.target_id == "app:brave").weight == (
        pytest.approx(0.19)  # 0.1 + 0.1 * (1 - 0.1)
    )


async def test_wire_cooccurrence_skips_a_tab_and_its_own_browser(tmp_path) -> None:
    """V-1 · a `PART_OF`-linked pair is structure: no Hebbian weight lands on
    the `PART_OF` edge and no association edge is added beside it."""
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    await gm.add_node("webapp:youtube", NodeType.WEBAPP, None)
    await gm.add_edge("webapp:youtube", "app:brave", RelationType.PART_OF)

    assert await gm.wire_cooccurrence(["webapp:youtube", "app:brave"], delta=0.1) == (0, 0)
    assert [(e.relation, e.weight) for e in gm.get_edges("webapp:youtube")] == [
        (RelationType.PART_OF, 0.0)
    ]


async def test_wire_coactivation_is_star_shaped_and_scaled(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    for nid in ("app:focus", "app:p1", "app:p2"):
        await gm.add_node(nid, NodeType.APP, None)

    created, bumped = await gm.wire_coactivation(
        "app:focus", [("app:p1", 1.0), ("app:p2", 0.5)], rate=0.1
    )
    assert (created, bumped) == (2, 0)

    def w(a: str, b: str) -> float:
        return next(e for e in gm.get_edges(a) if e.target_id == b).weight

    assert w("app:focus", "app:p1") == pytest.approx(0.1)
    assert w("app:focus", "app:p2") == pytest.approx(0.05)  # half the credit
    assert not any(e.target_id == "app:p2" for e in gm.get_edges("app:p1"))  # no peer<->peer


async def test_wire_coactivation_ignores_non_activity_and_absent_nodes(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:focus", NodeType.APP, None)
    await gm.add_node("file:/x.py", NodeType.FILE, None)
    peers = [("file:/x.py", 1.0), ("app:ghost", 1.0), ("app:focus", 1.0)]
    assert await gm.wire_coactivation("app:focus", peers, rate=0.1) == (0, 0)
    assert await gm.wire_coactivation("file:/x.py", [("app:focus", 1.0)], rate=0.1) == (0, 0)
    assert gm.get_edges("app:focus") == []


async def test_decay_heals_stray_weight_on_structural_edges(tmp_path) -> None:
    """V-1 · pre-fix builds bumped `PART_OF` weight, which decay never touched
    — the sweep now resets it."""
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    await gm.add_node("webapp:youtube", NodeType.WEBAPP, None)
    await gm.add_edge("webapp:youtube", "app:brave", RelationType.PART_OF, weight=0.04)

    await gm.decay_cooccurrence_edges(1.0, 0.02)
    (edge,) = gm.get_edges("webapp:youtube")
    assert edge.weight == 0.0


async def test_wire_cooccurrence_caps_episode_size(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    apps = [f"app:a{i}" for i in range(10)]
    for nid in apps:
        await gm.add_node(nid, NodeType.APP, None)

    created, _ = await gm.wire_cooccurrence(apps, delta=0.01, max_episode=4)
    assert created == 6  # only the first 4 ids -> C(4,2)


async def test_wire_cooccurrence_caps_new_edges_per_call(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    apps = [f"app:b{i}" for i in range(8)]
    for nid in apps:
        await gm.add_node(nid, NodeType.APP, None)

    created, _ = await gm.wire_cooccurrence(apps, delta=0.01, max_episode=8, max_new_edges=5)
    assert created == 5  # C(8,2) == 28 possible, capped at 5


async def test_wire_cooccurrence_only_creates_between_activity_nodes(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:zed", NodeType.APP, None)
    await gm.add_node("file:/x.py", NodeType.FILE, None)
    await gm.add_node("concept:flow", NodeType.CONCEPT, None)

    created, bumped = await gm.wire_cooccurrence(
        ["app:zed", "file:/x.py", "concept:flow"], delta=0.01
    )
    assert (created, bumped) == (0, 0)
    assert gm.get_edges("app:zed") == []

    # an existing RELATED_TO edge to a non-activity node is still strengthened
    await gm.add_edge("app:zed", "file:/x.py", RelationType.RELATED_TO, weight=0.1)
    _, bumped2 = await gm.wire_cooccurrence(["app:zed", "file:/x.py"], delta=0.01)
    assert bumped2 == 1
    # ...but a PART_OF edge between them never carries Hebbian weight
    await gm.add_edge("file:/x.py", "concept:flow", RelationType.PART_OF)
    _, bumped3 = await gm.wire_cooccurrence(["file:/x.py", "concept:flow"], delta=0.01)
    assert bumped3 == 0


async def test_wire_cooccurrence_skips_absent_ids(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:zed", NodeType.APP, None)
    created, bumped = await gm.wire_cooccurrence(["app:zed", "app:ghost"], delta=0.01)
    assert (created, bumped) == (0, 0)


async def test_decay_cooccurrence_edges_fades_and_prunes(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    for nid in ("app:a", "app:b", "app:c", "app:d"):
        await gm.add_node(nid, NodeType.APP, None)
    # a<->b strong, c<->d weak; every node also pinned to a hub so a prune of the
    # weak edge does not leave c or d an orphan
    await gm.add_edge("app:a", "app:b", RelationType.RELATED_TO, weight=0.50)
    await gm.add_edge("app:c", "app:d", RelationType.RELATED_TO, weight=0.03)
    for nid in ("app:a", "app:b", "app:c", "app:d"):
        await gm.add_edge(nid, "domain:engineering", RelationType.PART_OF)

    pruned = await gm.decay_cooccurrence_edges(0.9, 0.02)
    assert pruned == 0
    assert next(e for e in gm.get_edges("app:a") if e.target_id == "app:b").weight == (
        pytest.approx(0.45)
    )
    # 0.03 -> 0.027, still above the floor
    pruned = await gm.decay_cooccurrence_edges(0.5, 0.02)  # 0.027 -> 0.0135 < floor
    assert pruned == 1
    assert not any(e.target_id == "app:d" for e in gm.get_edges("app:c"))


async def test_decay_cooccurrence_never_reorphans_a_node(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("app:a", NodeType.APP, None)
    await gm.add_node("app:b", NodeType.APP, None)
    await gm.add_edge("app:a", "app:b", RelationType.RELATED_TO, weight=0.03)  # their only edge

    pruned = await gm.decay_cooccurrence_edges(0.1, 0.02)  # 0.003 < floor
    assert pruned == 0  # would orphan both a and b
    assert gm.get_edges("app:a")  # edge kept


async def test_decay_cooccurrence_leaves_structural_related_to_alone(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    await gm.add_node("insight:x", NodeType.INSIGHT, None)
    await gm.add_node("app:a", NodeType.APP, None)
    await gm.add_edge("insight:x", "app:a", RelationType.RELATED_TO)  # weight 0.0

    await gm.decay_cooccurrence_edges(0.9, 0.02)
    edge = next(e for e in gm.get_edges("insight:x") if e.target_id == "app:a")
    assert edge.weight == pytest.approx(0.0)  # untouched — only weight > 0 decays


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


async def test_warm_activity_peers_finds_recent_apps_within_the_window(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    at = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    await gm.add_node("app:terminal", NodeType.APP, {"label": "Terminal"})
    await gm.add_node("app:stale", NodeType.APP, {"label": "Stale"})
    await gm.add_node("file:/x", NodeType.FILE, {"label": "x"})
    await gm.mark_seen("app:terminal", at - timedelta(seconds=30))
    await gm.mark_seen("app:stale", at - timedelta(hours=2))  # outside a 300s window
    await gm.mark_seen("file:/x", at)  # not an app/webapp node — excluded regardless

    warm = dict(gm.warm_activity_peers("app:code", at, 300.0))
    assert warm.keys() == {"app:terminal"}
    assert warm["app:terminal"] == pytest.approx(1.0 - 30.0 / 300.0)


async def test_warm_activity_peers_excludes_the_focus_node_itself(tmp_path) -> None:
    gm = await _loaded_graph(tmp_path)
    at = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    await gm.add_node("app:code", NodeType.APP, {"label": "Code"})
    await gm.mark_seen("app:code", at)
    assert gm.warm_activity_peers("app:code", at, 300.0) == []


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


# gen-ref: 6ac6e655
