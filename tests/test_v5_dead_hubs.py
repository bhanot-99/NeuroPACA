# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-5 · five dead domain hubs (VISION.md §5).

`domain:system`, `domain:projects`, `domain:meetings`, `domain:comms` and
`domain:mental_models` sat at degree 0 in the live graph: dead weight in every
graph view and an empty branch in `find_related`. Two different reasons, worth
keeping straight —

* `comms` / `projects` / `meetings` are mapped in the shipped map files; they
  were empty only because those apps had not been opened yet;
* `system` / `mental_models` have **no entry in either map file**, so nothing
  could ever route to them — dead by construction.

The fix treats both the same way: a hub nothing reaches is reaped in the idle
sweep, and `_add_edge_unsafe` materialises one again the instant something
routes to it. These tests pin that a reaped hub is genuinely one edge from
coming back, and that `YOU` and live hubs are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.graph_memory import (
    DOMAIN_HUB_IDS,
    DOMAIN_SLUGS,
    HUB_NODE_IDS,
    GraphMemory,
)


async def _graph(tmp_path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


# ===================================================== 1 · the sweep reaps them


async def test_a_fresh_graph_loses_every_hub_nothing_routes_to(tmp_path) -> None:
    gm = await _graph(tmp_path)
    assert gm.node_count == 11  # a first run still seeds all 11 — self-describing

    dropped = await gm.prune_dead_hubs()

    assert dropped == len(DOMAIN_SLUGS)
    assert gm.node_ids == ["YOU"]


async def test_a_hub_something_routes_to_survives(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:code", NodeType.APP, {"label": "VS Code"})
    await gm.add_edge("app:code", "domain:engineering", RelationType.PART_OF)

    dropped = await gm.prune_dead_hubs()

    assert dropped == len(DOMAIN_SLUGS) - 1
    assert gm.get_node("domain:engineering") is not None
    assert gm.get_node("domain:tools") is None


async def test_you_is_never_reaped_even_at_degree_zero(tmp_path) -> None:
    """`YOU` is the anchor `link_orphan_nodes` attaches true orphans to —
    reaping it would strand every orphan the next sweep finds."""
    gm = await _graph(tmp_path)
    await gm.prune_dead_hubs()
    assert gm.get_node("YOU") is not None
    # and it still works as the anchor afterwards
    await gm.add_node("app:new", NodeType.APP, None)
    assert await gm.link_orphan_nodes() == 1
    assert gm.get_node("YOU") is not None


async def test_reaping_is_idempotent(tmp_path) -> None:
    gm = await _graph(tmp_path)
    assert await gm.prune_dead_hubs() == len(DOMAIN_SLUGS)
    assert await gm.prune_dead_hubs() == 0


async def test_a_hub_emptied_later_is_reaped_then(tmp_path) -> None:
    """The case never-seeding alone would miss: the app is uninstalled, or its
    mapping is removed, and the hub goes dead months into a graph's life."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:zoom", NodeType.APP, {"label": "Zoom"})
    await gm.add_edge("app:zoom", "domain:meetings", RelationType.PART_OF)
    await gm.prune_dead_hubs()
    assert gm.get_node("domain:meetings") is not None

    await gm.delete_node("app:zoom")

    assert await gm.prune_dead_hubs() == 1
    assert gm.get_node("domain:meetings") is None


# ============================================ 2 · and they come straight back


async def test_a_reaped_hub_is_materialised_by_the_first_edge_that_routes_to_it(
    tmp_path,
) -> None:
    gm = await _graph(tmp_path)
    await gm.prune_dead_hubs()
    assert gm.get_node("domain:comms") is None

    await gm.add_node("app:slack", NodeType.APP, {"label": "Slack"})
    await gm.add_edge("app:slack", "domain:comms", RelationType.PART_OF)

    hub = gm.get_node("domain:comms")
    assert hub is not None
    assert hub.node_type is NodeType.CONCEPT
    assert hub.label == "Comms"  # identical to what the fresh-graph seed makes


async def test_a_materialised_hub_matches_the_seeded_one_for_every_slug(tmp_path) -> None:
    """Labels are what a graph view shows; a hub that came back looking
    different from the one it replaced would be a visible regression."""
    seeded = await _graph(tmp_path)
    expected = {hub: seeded.get_node(hub).label for hub in sorted(DOMAIN_HUB_IDS)}
    await seeded.prune_dead_hubs()
    for i, hub in enumerate(sorted(DOMAIN_HUB_IDS)):
        await seeded.add_node(f"app:{i}", NodeType.APP, None)
        await seeded.add_edge(f"app:{i}", hub, RelationType.PART_OF)
    assert {hub: seeded.get_node(hub).label for hub in sorted(DOMAIN_HUB_IDS)} == expected


async def test_materialising_works_from_either_end_of_the_edge(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.prune_dead_hubs()
    await gm.add_node("app:x", NodeType.APP, None)

    await gm.add_edge("domain:tools", "app:x", RelationType.RELATED_TO)

    assert gm.get_node("domain:tools") is not None


async def test_a_materialised_hub_is_a_real_node_not_a_networkx_placeholder(
    tmp_path,
) -> None:
    """networkx would silently create an attribute-less node for an unknown
    edge endpoint; every later read of it raises deep inside `_node_from_attrs`.
    A save/load round trip is the strictest check that it is well-formed."""
    gm = await _graph(tmp_path)
    await gm.prune_dead_hubs()
    await gm.add_node("app:y", NodeType.APP, None)
    await gm.add_edge("app:y", "domain:research", RelationType.PART_OF)
    await gm.save()

    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await reloaded.load()

    assert reloaded.get_node("domain:research") is not None
    assert reloaded.get_node("domain:research").label == "Research"


# ================================================ 3 · the map files, as shipped


@pytest.mark.parametrize("name", ["app_map.default.toml", "webapp_map.default.toml"])
def test_shipped_maps_only_name_known_domains(name: str) -> None:
    """Guards the other half of V-5: a typo'd domain in a map file routes
    nowhere and would show up as yet another permanently dead hub."""
    import tomllib

    raw = tomllib.loads((Path("data") / name).read_text("utf-8"))
    used = {v for section in raw.values() if isinstance(section, dict) for v in section.values()}
    assert used <= set(DOMAIN_SLUGS), (
        f"{name} routes to unknown domain(s): {used - set(DOMAIN_SLUGS)}"
    )


def test_the_unroutable_domains_are_still_a_valid_vocabulary() -> None:
    """`system` and `mental_models` have no mapping anywhere, so their hubs can
    never be reached today — but the slugs stay valid so a user's own map file
    may route to them. That is exactly why the fix reaps rather than deletes
    them from `DOMAIN_SLUGS`."""
    assert "system" in DOMAIN_SLUGS
    assert "mental_models" in DOMAIN_SLUGS
    assert HUB_NODE_IDS == DOMAIN_HUB_IDS | {"YOU"}
