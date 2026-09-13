# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for ReadingListPlugin (S4 · domain:learning).

Verifies JSON and Markdown reading list parsing, side-effect-free plugin contract,
PluginHost integration, V-10 non-inflation, and domain hub linking.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from plugins.reading.reading_plugin import ReadingListPlugin

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.sensing.plugin_host import PluginHost


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 13, 15, 0, tzinfo=UTC))


async def test_reading_list_json_parsing(tmp_path: Path) -> None:
    data = [
        {
            "id": "ddia",
            "title": "Designing Data-Intensive Applications",
            "author": "Martin Kleppmann",
            "status": "reading",
            "progress_pct": 65,
        },
        {
            "id": "crafting-interpreters",
            "title": "Crafting Interpreters",
            "author": "Robert Nystrom",
            "status": "completed",
            "progress_pct": 100,
        },
    ]
    json_file = tmp_path / "reading_list.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")

    plugin = ReadingListPlugin(json_file)
    desc = plugin.describe()
    assert desc.name == "reading_list"
    assert desc.domain_hub == "domain:learning"
    assert desc.node_type == NodeType.CONCEPT

    items = await plugin.items(datetime(1970, 1, 1, tzinfo=UTC))
    assert len(items) == 2

    ddia = next(it for it in items if "ddia" in it.entity_id)
    assert ddia.label == "Designing Data-Intensive Applications"
    assert ddia.fact is not None
    assert ddia.fact[2]["author"] == "Martin Kleppmann"
    assert ddia.fact[2]["progress"] == 65
    assert (ddia.entity_id, "domain:learning", RelationType.PART_OF) in ddia.edges


async def test_reading_list_markdown_checklist_parsing(tmp_path: Path) -> None:
    md_content = """# My Reading List
- [x] Structure and Interpretation of Computer Programs by Hal Abelson
- [ ] Computer Systems: A Programmer's Perspective by Randal Bryant
"""
    md_file = tmp_path / "reading_list.md"
    md_file.write_text(md_content, encoding="utf-8")

    plugin = ReadingListPlugin(md_file)
    items = await plugin.items(datetime(1970, 1, 1, tzinfo=UTC))
    assert len(items) == 2

    sicp = next(it for it in items if "structure" in it.entity_id)
    assert sicp.fact is not None
    assert sicp.fact[2]["status"] == "completed"
    assert sicp.fact[2]["author"] == "Hal Abelson"

    csapp = next(it for it in items if "computer-systems" in it.entity_id)
    assert csapp.fact is not None
    assert csapp.fact[2]["status"] == "reading"
    assert csapp.fact[2]["author"] == "Randal Bryant"


async def test_reading_plugin_host_integration_and_v10(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    data = [
        {
            "id": "book-distributed-systems",
            "title": "Distributed Systems",
            "author": "Maarten van Steen",
            "status": "reading",
            "progress_pct": 40,
        }
    ]
    json_file = tmp_path / "reading.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("domain:learning", NodeType.CONCEPT, attributes={"label": "Learning"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    plugin = ReadingListPlugin(json_file)
    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=fake_clock)
    await host.initialize()

    try:
        # Tick 1: ingest reading list
        facts = await host.poll_tick()
        assert facts == 1
        await store.flush()

        entity_id = "reading:book-distributed-systems"
        assert gm.has_node(entity_id)
        node = gm.get_node(entity_id)
        assert node is not None
        assert node.label == "Distributed Systems"
        initial_access_count = node.access_count

        # Edge to domain:learning
        assert any(
            e.target_id == "domain:learning" and e.relation == RelationType.PART_OF
            for e in gm.get_edges(entity_id)
        )

        # Plugin fact asserted
        records = await store.at(fake_clock.now())
        plugin_facts = [r for r in records if r.kind == str(EpisodeKind.PLUGIN_FACT)]
        assert len(plugin_facts) == 1
        assert plugin_facts[0].subject == entity_id
        assert plugin_facts[0].attrs["progress"] == 40

        # Tick 2: repeat poll unchanged -> churn suppressed
        await fake_clock.advance(60.0)
        facts2 = await host.poll_tick()
        assert facts2 == 0

        # V-10 non-inflation: access_count unchanged by polling
        node_after = gm.get_node(entity_id)
        assert node_after is not None
        assert node_after.access_count == initial_access_count

        # Forget cleans up
        removed = await host.forget(entity_id)
        assert removed >= 1
        assert not gm.has_node(entity_id)
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
