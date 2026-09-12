# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-6 · isolated nodes never get linked (VISION.md §5).

`link_orphan_nodes` only ever ran inside a DMN reminiscence cycle, so a node
created after the last idle spell floated unreachable — invisible to
`find_related` from anything — until the CPU next went quiet. On a machine in
continuous use that is hours, and a restart in between did not help. The live
graph's example was `app:cosmic-settings`: score 0.0, degree 0, focused at 16:40
and still unreachable that evening.

The fix is a ledger, not a faster scan: `_add_node_unsafe` records a new id,
`_add_edge_unsafe` discharges it the moment an edge lands, and the scheduler
drains what is left every tick. These tests pin the promptness, the O(pending)
cost, the bound on the ledger, and that the whole-graph sweep still works as the
backstop for anything the ledger cannot know about.
"""

from __future__ import annotations

from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.graph_memory import _UNLINKED_LEDGER_CAP, GraphMemory
from neuropaca.orchestration.scheduler import Scheduler


async def _graph(tmp_path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


def _degree(gm: GraphMemory, node_id: str) -> int:
    return len(gm.get_edges(node_id))


# ====================================================== 1 · a new node is linked


async def test_a_node_minted_with_no_edge_is_linked_on_the_next_pass(tmp_path) -> None:
    """The `app:cosmic-settings` case: an unmapped app the correlator mints on
    focus has no `part_of domain:` edge and no co-active peer to wire to."""
    gm = await _graph(tmp_path)
    await gm.upsert_node("app:cosmic-settings", NodeType.APP, {"label": "Cosmic Settings"})
    assert _degree(gm, "app:cosmic-settings") == 0

    assert await gm.link_new_orphans() == 1

    edges = gm.get_edges("app:cosmic-settings")
    assert [(e.target_id, e.relation) for e in edges] == [("YOU", RelationType.RELATED_TO)]


async def test_a_node_that_already_gained_an_edge_is_not_linked(tmp_path) -> None:
    """A mapped app gets its domain edge microseconds after creation; it must
    never collect a `-> YOU` placeholder that V-3a then has to hand back."""
    gm = await _graph(tmp_path)
    await gm.upsert_node("app:code", NodeType.APP, {"label": "VS Code"})
    await gm.add_edge("app:code", "domain:engineering", RelationType.PART_OF)

    assert await gm.link_new_orphans() == 0
    assert all(e.target_id != "YOU" for e in gm.get_edges("app:code"))


async def test_an_edge_discharges_both_of_its_endpoints(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.upsert_node("app:a", NodeType.APP, None)
    await gm.upsert_node("app:b", NodeType.APP, None)
    await gm.add_edge("app:a", "app:b", RelationType.RELATED_TO, weight=0.3)

    assert await gm.link_new_orphans() == 0
    assert gm._unlinked == {}


async def test_a_deleted_node_is_never_linked(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.upsert_node("app:gone", NodeType.APP, None)
    await gm.delete_node("app:gone")

    assert await gm.link_new_orphans() == 0
    assert gm.get_node("app:gone") is None


async def test_the_pass_is_idempotent_and_drains_the_ledger(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.upsert_node("app:x", NodeType.APP, None)

    assert await gm.link_new_orphans() == 1
    assert await gm.link_new_orphans() == 0
    assert gm._unlinked == {}


async def test_hubs_never_enter_the_ledger(tmp_path) -> None:
    """The 10 domain hubs are minted edgeless on a fresh graph; linking them to
    `YOU` would re-create exactly the hub-and-spoke tangle V-3a removed."""
    gm = await _graph(tmp_path)
    assert gm._unlinked == {}
    assert await gm.link_new_orphans() == 0


# ============================================================== 2 · what it costs


async def test_the_pass_does_not_scan_the_graph(tmp_path) -> None:
    """The whole point of the ledger: a tick every few minutes must not carry an
    O(N) degree walk of a 10k-node graph."""
    gm = await _graph(tmp_path)
    for i in range(400):
        await gm.upsert_node(f"file:/f{i}", NodeType.FILE, None)
        await gm.add_edge(f"file:/f{i}", "domain:engineering", RelationType.PART_OF)
    await gm.upsert_node("app:lonely", NodeType.APP, None)

    walked = 0
    real = gm._graph.degree

    def counting(*a, **kw):
        nonlocal walked
        walked += 1
        return real(*a, **kw)

    gm._graph.degree = counting  # type: ignore[method-assign]
    linked = await gm.link_new_orphans()
    gm._graph.degree = real  # type: ignore[method-assign]

    assert linked == 1
    assert walked == 1  # one degree lookup, for the one pending id


async def test_the_ledger_is_bounded(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for i in range(_UNLINKED_LEDGER_CAP + 50):
        await gm.upsert_node(f"file:/f{i}", NodeType.FILE, None)

    assert len(gm._unlinked) == _UNLINKED_LEDGER_CAP
    # FIFO: the oldest ids were evicted, the newest are still queued
    assert "file:/f0" not in gm._unlinked
    assert f"file:/f{_UNLINKED_LEDGER_CAP + 49}" in gm._unlinked


async def test_the_whole_graph_sweep_still_catches_anything_evicted(tmp_path) -> None:
    """The ledger is an optimisation, never the only guarantee — the DMN's
    `link_orphan_nodes` remains the backstop."""
    gm = await _graph(tmp_path)
    for i in range(_UNLINKED_LEDGER_CAP + 10):
        await gm.upsert_node(f"file:/f{i}", NodeType.FILE, None)
    await gm.link_new_orphans()

    assert _degree(gm, "file:/f0") == 0  # evicted from the ledger, still an orphan
    assert await gm.link_orphan_nodes() == 10
    assert _degree(gm, "file:/f0") == 1


# ======================================================== 3 · it runs on the tick


async def test_the_scheduler_tick_links_new_orphans(tmp_path) -> None:
    """Promptness is the whole fix: reachable within one tick rather than at the
    next idle spell."""
    gm = await _graph(tmp_path)
    scheduler = Scheduler(gm, Config(inference_backend="fake"))
    await gm.upsert_node("app:fresh", NodeType.APP, None)

    await scheduler._tick()

    assert _degree(gm, "app:fresh") == 1


async def test_a_tick_persists_the_link_it_just_made(tmp_path) -> None:
    """Linking after the save would leave the edge unpersisted for a whole
    interval — and lost entirely if the daemon stops in between."""
    gm = await _graph(tmp_path)
    scheduler = Scheduler(gm, Config(inference_backend="fake"))
    await gm.upsert_node("app:fresh", NodeType.APP, None)

    await scheduler._tick()

    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await reloaded.load()
    assert _degree(reloaded, "app:fresh") == 1


async def test_a_failing_link_pass_does_not_break_the_tick(tmp_path) -> None:
    """A tick is guarded (rules.md §2): the save and the rescore still have to
    happen if the new pass throws."""
    gm = await _graph(tmp_path)
    scheduler = Scheduler(gm, Config(inference_backend="fake"))

    async def boom() -> int:
        raise RuntimeError("ledger exploded")

    gm.link_new_orphans = boom  # type: ignore[method-assign]
    await scheduler._tick()  # logged, not raised


# gen-ref: 7f201b5d
