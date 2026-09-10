# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-3 · cruft accumulating in the graph.

- V-3a — `link_orphan_nodes` gave a briefly-orphaned node a `-> YOU`
  placeholder that nothing ever took back; `release_you_links` does, in the DMN
  sweep and at boot.
- V-3c — every shipped config overrode `process_exclude_names` with `[]`, so
  the daemon's own processes became `app:` nodes; the boot tidy now drops nodes
  for excluded names, and the configs no longer override the default.
- V-3d — census and focus names for one app land on one node at write time
  (a regression guard; the startup merge seen once was pre-B17 data).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.app_identity import AppIdentity
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.diagnosis.signal import NodeSpec, SignalDraft
from neuropaca.idle.dmn import DefaultModeNetwork
from neuropaca.orchestration.orchestrator import non_activity_app

_REPO = Path(__file__).resolve().parents[1]


async def _graph(tmp_path: Path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    return gm


def _you_linked(gm: GraphMemory, node_id: str) -> bool:
    return any(e.target_id == "YOU" for e in gm.get_edges(node_id) if e.source_id == node_id)


# --------------------------------------------------------------------- V-3a
async def test_release_you_links_only_frees_nodes_that_found_a_real_edge(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for nid in ("app:brave", "app:lonely"):
        await gm.add_node(nid, NodeType.APP, None)
    assert await gm.link_orphan_nodes() == 2  # both start as orphans
    await gm.add_edge("app:brave", "domain:habits", RelationType.PART_OF)  # classified later

    assert await gm.release_you_links() == 1
    assert not _you_linked(gm, "app:brave")
    assert _you_linked(gm, "app:lonely")  # still an orphan without it — kept
    assert await gm.release_you_links() == 0  # idempotent
    assert await gm.link_orphan_nodes() == 0  # and nothing was re-orphaned


async def test_release_you_links_leaves_a_weighted_you_edge_alone(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:x", NodeType.APP, None)
    await gm.add_edge("app:x", "YOU", RelationType.RELATED_TO, weight=0.4)  # not a placeholder
    await gm.add_edge("app:x", "domain:tools", RelationType.PART_OF)
    assert await gm.release_you_links() == 0
    assert _you_linked(gm, "app:x")


async def test_dmn_sweep_releases_placeholders_before_linking(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    dmn = DefaultModeNetwork(
        bus, Config(inference_backend="fake"), gm, BitNetRuntime.get_instance(), clock=FakeClock()
    )
    try:
        await gm.add_node("app:a", NodeType.APP, None)
        await gm.link_orphan_nodes()
        await gm.add_edge("app:a", "domain:engineering", RelationType.PART_OF)

        summary = await dmn._reminiscence()
        assert "released 1" in summary
        assert not _you_linked(gm, "app:a")
    finally:
        await bus.stop()


# --------------------------------------------------------------------- V-3c
@pytest.mark.parametrize(
    "name, expected",
    [
        ("neuropacad", True),  # process_exclude_names
        ("python3", True),
        ("cosmic-comp", True),
        ("zenity", True),  # focus_exclude_app_ids
        ("MainThread", True),  # B17 thread label
        ("cosmic-term", False),
        ("brave", False),
    ],
)
def test_non_activity_app_covers_both_exclude_lists(name: str, expected: bool) -> None:
    predicate = non_activity_app(Config(inference_backend="fake"), AppIdentity.empty())
    assert predicate(name) is expected


async def test_boot_tidy_drops_nodes_for_excluded_names(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for nid in ("app:neuropacad", "app:python3", "app:cosmic-term"):
        await gm.add_node(nid, NodeType.APP, None)
    await gm.link_orphan_nodes()
    identity = AppIdentity.empty()

    _, dropped = await gm.canonicalise_app_nodes(
        identity.resolve, non_activity_app(Config(inference_backend="fake"), identity)
    )
    assert dropped == 2
    assert gm.get_node("app:neuropacad") is None and gm.get_node("app:python3") is None
    assert gm.get_node("app:cosmic-term") is not None


@pytest.mark.parametrize(
    "config_file", ["neuropaca.toml", "neuropaca.b13.toml", "neuropaca.soak.toml"]
)
def test_shipped_configs_do_not_override_the_exclude_default(config_file: str) -> None:
    """V-3c's root cause: `process_exclude_names = []` in every shipped config
    silently replaced the D-20 default. Parsed directly — loading the config
    validates model paths CI does not have."""
    data = tomllib.loads((_REPO / config_file).read_text("utf-8"))
    assert "process_exclude_names" not in data


# --------------------------------------------------------------------- V-3d
async def test_census_and_focus_names_for_one_app_are_one_node(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    gm = await _graph(tmp_path)
    corr = SignalCorrelator(bus, Config(inference_backend="fake"), gm)
    await corr.initialize()
    await corr.start()
    try:
        # focus sensor: the Wayland app_id
        await corr.on_app_switch(
            Event(
                event_type=EventType.APP_SWITCH,
                source="sensing.activity",
                payload={"app_id": "com.system76.CosmicFiles", "webapp": None},
            )
        )
        # census pattern: the process name, written through the same chokepoint
        draft = SignalDraft(
            signal_type=SignalType.HIGH_LOAD,
            confidence=0.9,
            source_snapshots=(),
            node_specs=(NodeSpec("app:cosmic-files", NodeType.APP, "cosmic-files"),),
        )
        await corr._update_graph(draft)

        apps = [n for n in gm.node_ids if n.startswith("app:")]
        assert len(apps) == 1, apps
    finally:
        await corr.stop()
        await bus.stop()
