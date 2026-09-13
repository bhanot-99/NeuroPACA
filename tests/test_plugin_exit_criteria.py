# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S4 Exit Criteria Verification Tests (VISION_PHASES.md §S4).

Exit Criteria:
1. "A fourth domain touches only its own plugin directory."
2. "doctor flags any plugin exceeding its manifest."
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
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginHost,
    PluginItem,
    PluginManifest,
    doctor,
)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 13, 16, 0, tzinfo=UTC))


async def test_exit_criterion_1_fourth_domain_touches_only_its_own_plugin_dir(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Exit Criterion 1:

    A fourth domain (e.g. meetings/calendar) touches only its own plugin directory,
    implements the side-effect-free Plugin protocol, and integrates into GraphMemory
    and EpisodeStore via PluginHost with zero modifications to core code.
    """
    ics_file = tmp_path / "team_calendar.ics"
    ics_file.write_text(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:exit-crit-meeting-42@example.com
DTSTART:20260913T160000Z
DTEND:20260913T170000Z
SUMMARY:Sprint Retrospective
LOCATION:Conference Room B
END:VEVENT
END:VCALENDAR
""",
        encoding="utf-8",
    )

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("domain:meetings", NodeType.CONCEPT, attributes={"label": "Meetings"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    # The fourth domain plugin is imported exclusively from its own directory (plugins/calendar)
    plugin = CalendarPlugin(ics_file)
    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=fake_clock)
    await host.initialize()

    try:
        # Ingest the fourth domain via the standard PluginHost
        facts = await host.poll_tick()
        assert facts == 1
        await store.flush()

        entity_id = "event:exit-crit-meeting-42-example-com"

        # 1. Graph node exists in the fourth domain
        assert gm.has_node(entity_id)
        node = gm.get_node(entity_id)
        assert node is not None
        assert node.label == "Sprint Retrospective"

        # 2. Linked directly to domain:meetings
        assert any(
            e.target_id == "domain:meetings" and e.relation == RelationType.PART_OF
            for e in gm.get_edges(entity_id)
        )

        # 3. Meeting span recorded in EpisodeStore
        records = await store.between(
            datetime(2026, 9, 13, 15, 59, tzinfo=UTC),
            datetime(2026, 9, 13, 17, 1, tzinfo=UTC),
        )
        spans = [r for r in records if r.kind == str(EpisodeKind.FOCUS_SPAN)]
        assert len(spans) == 1
        assert spans[0].subject == entity_id

        # 4. Meeting fact recorded in EpisodeStore
        facts_recorded = [r for r in records if r.kind == str(EpisodeKind.TOPIC_FACT)]
        assert len(facts_recorded) == 1
        assert facts_recorded[0].attrs["location"] == "Conference Room B"
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_exit_criterion_2_doctor_flags_plugin_exceeding_manifest(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Exit Criterion 2:

    `doctor` flags any plugin exceeding its declared manifest (unauthorized paths
    or forbidden network access), and host.health() reflects the violations.
    """
    allowed_dir = tmp_path / "allowed_sandbox"
    allowed_dir.mkdir(parents=True)

    class RoguePlugin:
        """A rogue plugin trying to exceed its manifest."""

        def __init__(self) -> None:
            self.calendar_path = str(tmp_path / "system" / "shadow_passwords.txt")
            self.network_url = "https://exfiltrate.malicious.org"

        def describe(self) -> PluginDescriptor:
            return PluginDescriptor(
                name="rogue",
                node_type=NodeType.CONCEPT,
                domain_hub="domain:tools",
                poll_interval_seconds=60.0,
                manifest=PluginManifest(
                    allowed_read_paths=(allowed_dir,),
                    allow_network=False,
                ),
            )

        async def items(self, since: datetime) -> list[PluginItem]:
            return []

        def entities(self) -> frozenset[str]:
            return frozenset()

        async def forget(self, entity: str) -> int:
            return 0

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    plugin = RoguePlugin()
    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=fake_clock)
    await host.initialize()

    try:
        # Run doctor check
        violations = doctor(host)

        # Flag 1: unauthorized path outside allowed_read_paths
        assert any(
            "rogue" in v and "exceeds manifest allowed_read_paths" in v for v in violations
        ), f"Missing path violation in {violations}"

        # Flag 2: prohibited network access
        assert any(
            "rogue" in v and "network access 'https://exfiltrate.malicious.org' prohibited" in v
            for v in violations
        ), f"Missing network violation in {violations}"

        # Module health must report degraded/unhealthy
        health = host.health()
        assert health.ok is False
        assert "manifest violations" in health.detail
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
