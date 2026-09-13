# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for CalendarPlugin (S4 · domain:meetings).

Verifies RFC 5545 .ics parsing, side-effect-free plugin contract,
PluginHost integration, V-10 non-inflation, and domain hub linking.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from plugins.calendar.calendar_plugin import CalendarPlugin

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.sensing.plugin_host import PluginHost

_SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//NeuroPaca Test//EN
BEGIN:VEVENT
UID:sync-20260913-001@example.com
DTSTART:20260913T100000Z
DTEND:20260913T103000Z
SUMMARY:Project Architecture Sync
LOCATION:Room 101
DESCRIPTION:Weekly architectural alignment meeting with long description
 that spans multiple lines per RFC 5545 unfolding specification.
END:VEVENT
BEGIN:VEVENT
UID:lunch-20260913-002@example.com
DTSTART:20260913T120000Z
DTEND:20260913T130000Z
SUMMARY:Team Lunch
LOCATION:Cafeteria
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 13, 14, 0, tzinfo=UTC))


async def test_calendar_ics_parsing(tmp_path: Path) -> None:
    ics_file = tmp_path / "meetings.ics"
    ics_file.write_text(_SAMPLE_ICS, encoding="utf-8")

    plugin = CalendarPlugin(ics_file)
    desc = plugin.describe()
    assert desc.name == "calendar"
    assert desc.domain_hub == "domain:meetings"
    assert desc.node_type == NodeType.CONCEPT

    items = await plugin.items(datetime(1970, 1, 1, tzinfo=UTC))
    assert len(items) == 2

    sync_item = next(it for it in items if "sync" in it.entity_id)
    assert sync_item.label == "Project Architecture Sync"
    assert sync_item.node_type == NodeType.CONCEPT
    assert sync_item.span is not None
    assert sync_item.span[0] == datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
    assert sync_item.span[1] == datetime(2026, 9, 13, 10, 30, tzinfo=UTC)
    assert sync_item.span_kind == EpisodeKind.MEETING_SPAN

    # Check unfolded description
    assert sync_item.fact is not None
    assert "spans multiple lines per RFC 5545" in sync_item.fact[2]["description"]
    assert sync_item.fact[2]["location"] == "Room 101"

    # Edge to domain:meetings
    assert (sync_item.entity_id, "domain:meetings", RelationType.PART_OF) in sync_item.edges


async def test_calendar_plugin_host_integration_and_v10(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    ics_file = tmp_path / "calendar.ics"
    ics_file.write_text(_SAMPLE_ICS, encoding="utf-8")

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("domain:meetings", NodeType.CONCEPT, attributes={"label": "Meetings"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    plugin = CalendarPlugin(ics_file)
    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=fake_clock)
    await host.initialize()

    try:
        # Tick 1: ingest calendar
        facts_written = await host.poll_tick()
        assert facts_written == 2
        await store.flush()

        sync_entity = "event:sync-20260913-001-example-com"
        assert gm.has_node(sync_entity)
        node = gm.get_node(sync_entity)
        assert node is not None
        assert node.label == "Project Architecture Sync"
        initial_access_count = node.access_count

        # Hub linkage verified
        edges = gm.get_edges(sync_entity)
        assert any(
            e.target_id == "domain:meetings" and e.relation == RelationType.PART_OF for e in edges
        )

        # Spans recorded in EpisodeStore
        records = await store.between(
            datetime(2026, 9, 13, 9, 59, tzinfo=UTC),
            datetime(2026, 9, 13, 10, 31, tzinfo=UTC),
        )
        spans = [r for r in records if r.kind == str(EpisodeKind.MEETING_SPAN)]
        assert len(spans) == 1
        assert spans[0].subject == sync_entity

        # Tick 2: repeat poll with unchanged file
        await fake_clock.advance(60.0)
        facts2 = await host.poll_tick()
        assert facts2 == 0  # Churn suppressed

        # V-10 non-inflation: access_count unchanged by polling
        node_after = gm.get_node(sync_entity)
        assert node_after is not None
        assert node_after.access_count == initial_access_count

        # Forget cleans up entity across graph, store, and host
        removed = await host.forget(sync_entity)
        assert removed >= 1
        assert not gm.has_node(sync_entity)
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
