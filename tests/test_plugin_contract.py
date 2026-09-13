# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for the S4 Plugin Contract and PluginHost (VISION_PHASES.md).

Tests the generic contract once against a mock/fake plugin, proving:
1. V-10 invariance: first poll creates node; repeat polls call mark_seen only
   (access_count / relevance_score never inflate from polling).
2. Churn suppression: unchanged state_key emits zero new facts.
3. Force-refresh on span close: active session position is updated at stop time.
4. Discrete spans: closed historical spans land in EpisodeStore.
5. Event publication: item.events publish to EventBus.
6. Forget: entity scrub across graph, store, and host cache.
7. Manifest enforcement: startup validation flags plugins exceeding allowed paths
   or attempting network access when prohibited.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.sensing.plugin_host import (
    Plugin,
    PluginDescriptor,
    PluginHost,
    PluginItem,
    PluginManifest,
)


class FakeSensingPlugin:
    """A pure, side-effect-free test plugin implementing Plugin."""

    def __init__(
        self,
        name: str = "fake_sensor",
        node_type: NodeType = NodeType.CONCEPT,
        domain_hub: str = "domain:tools",
        poll_interval: float = 60.0,
        span_kind: EpisodeKind | None = EpisodeKind.FOCUS_SPAN,
        manifest: PluginManifest | None = None,
    ) -> None:
        self._desc = PluginDescriptor(
            name=name,
            node_type=node_type,
            domain_hub=domain_hub,
            poll_interval_seconds=poll_interval,
            span_kind=span_kind,
            manifest=manifest or PluginManifest(),
        )
        self.items_to_return: list[PluginItem] = []
        self._entities: set[str] = set()
        self.forgotten: list[str] = []
        self.calendar_path: str | None = None
        self.network_url: str | None = None

    def describe(self) -> PluginDescriptor:
        return self._desc

    async def items(self, since: datetime) -> list[PluginItem]:
        self._entities.update(it.entity_id for it in self.items_to_return)
        return list(self.items_to_return)

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    async def forget(self, entity: str) -> int:
        self.forgotten.append(entity)
        self._entities.discard(entity)
        return 1


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 13, 12, 0, tzinfo=UTC))


async def _setup_host(
    tmp_path: Path, clock: FakeClock, plugin: Plugin
) -> tuple[PluginHost, GraphMemory, EpisodeStore, EventBus]:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("domain:tools", NodeType.CONCEPT, attributes={"label": "Tools"})

    store = EpisodeStore(tmp_path / "episodes.db")
    await store.start()

    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=clock)
    await host.initialize()
    return host, gm, store, bus


async def test_plugin_protocol_conformance() -> None:
    plugin = FakeSensingPlugin()
    assert isinstance(plugin, Plugin)


async def test_v10_non_inflation_and_hub_connection(tmp_path: Path, fake_clock: FakeClock) -> None:
    plugin = FakeSensingPlugin()
    host, gm, store, bus = await _setup_host(tmp_path, fake_clock, plugin)
    try:
        plugin.items_to_return = [
            PluginItem(
                entity_id="tool:calculator",
                label="Calculator",
                fact=(EpisodeKind.TOPIC_FACT, "math", {"active": True}),
            )
        ]

        # Tick 1: Node creation
        facts = await host.poll_tick()
        assert facts == 1
        assert gm.has_node("tool:calculator")
        node = gm.get_node("tool:calculator")
        assert node is not None
        assert node.label == "Calculator"
        initial_access_count = node.access_count

        # Edge to domain:tools
        assert any(
            e.target_id == "domain:tools" and e.relation == RelationType.PART_OF
            for e in gm.get_edges("tool:calculator")
        )

        # Tick 2: Repeat poll with same item
        await fake_clock.advance(60.0)
        facts2 = await host.poll_tick()
        assert facts2 == 0  # Churn suppression: fact not re-written

        # V-10: access_count is NOT incremented by polling
        node_after = gm.get_node("tool:calculator")
        assert node_after is not None
        assert node_after.access_count == initial_access_count
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_force_refresh_on_span_close(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Ongoing active session refreshes its stopping position when span closes."""
    plugin = FakeSensingPlugin()
    host, _gm, store, bus = await _setup_host(tmp_path, fake_clock, plugin)
    try:
        # Active session tick 1: starts at pos 10.0s
        plugin.items_to_return = [
            PluginItem(
                entity_id="session:interactive_work",
                label="Interactive Session",
                active=True,
                fact=(
                    EpisodeKind.MEDIA_POSITION_FACT,
                    "ep1",
                    {"show": "Work", "position_seconds": 10.0},
                ),
                state_key=("work", 1),
            )
        ]
        assert (await host.poll_tick()) == 1

        # Active session tick 2: position moves to 40.0s, state_key identical -> suppressed
        await fake_clock.advance(30.0)
        plugin.items_to_return = [
            PluginItem(
                entity_id="session:interactive_work",
                label="Interactive Session",
                active=True,
                fact=(
                    EpisodeKind.MEDIA_POSITION_FACT,
                    "ep1",
                    {"show": "Work", "position_seconds": 40.0},
                ),
                state_key=("work", 1),
            )
        ]
        assert (await host.poll_tick()) == 0

        # Active session ends (active=False) with final position 75.0s
        await fake_clock.advance(35.0)
        plugin.items_to_return = [
            PluginItem(
                entity_id="session:interactive_work",
                label="Interactive Session",
                active=False,
                fact=(
                    EpisodeKind.MEDIA_POSITION_FACT,
                    "ep1",
                    {"show": "Work", "position_seconds": 75.0},
                ),
                state_key=("work", 1),
            )
        ]
        assert (await host.poll_tick()) == 1  # Force-refreshed on span close!
        await store.flush()

        # Check recorded span and updated fact in store
        records = await store.at(fake_clock.now())
        spans = [r for r in records if r.kind == str(EpisodeKind.FOCUS_SPAN)]
        assert len(spans) == 1
        assert spans[0].subject == "session:interactive_work"

        # Check latest position fact has 75.0s, not 10.0s
        facts = [r for r in records if r.kind == str(EpisodeKind.MEDIA_POSITION_FACT)]
        assert len(facts) == 1
        assert facts[0].attrs["position_seconds"] == 75.0
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_discrete_spans_and_event_publishing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    plugin = FakeSensingPlugin()
    host, _gm, store, bus = await _setup_host(tmp_path, fake_clock, plugin)
    events_received: list[Event] = []

    async def on_event(e: Event) -> None:
        events_received.append(e)

    bus.subscribe(EventType.USER_MESSAGE, on_event)

    try:
        t0 = fake_clock.now() - timedelta(minutes=10)
        t1 = fake_clock.now() - timedelta(minutes=5)
        ev = Event(event_type=EventType.USER_MESSAGE, source="test", payload={"text": "hello"})

        plugin.items_to_return = [
            PluginItem(
                entity_id="task:discrete_item",
                label="Discrete Item",
                span=(t0, t1),
                span_kind=EpisodeKind.FOCUS_SPAN,
                span_attrs={"result": "done"},
                events=(ev,),
            )
        ]

        await host.poll_tick()
        await store.flush()
        await bus.join()

        # Check span landed
        records = await store.between(t0 - timedelta(seconds=1), t1 + timedelta(seconds=1))
        spans = [r for r in records if r.kind == str(EpisodeKind.FOCUS_SPAN)]
        assert len(spans) == 1
        assert spans[0].attrs["result"] == "done"

        # Check event published
        assert len(events_received) == 1
        assert events_received[0].payload["text"] == "hello"
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_host_forget(tmp_path: Path, fake_clock: FakeClock) -> None:
    plugin = FakeSensingPlugin()
    host, gm, store, bus = await _setup_host(tmp_path, fake_clock, plugin)
    try:
        plugin.items_to_return = [
            PluginItem(
                entity_id="tool:doomed",
                label="Doomed Tool",
                fact=(EpisodeKind.TOPIC_FACT, "topic", {}),
            )
        ]
        await host.poll_tick()
        assert gm.has_node("tool:doomed")

        cleaned = await host.forget("tool:doomed")
        assert cleaned >= 1
        assert not gm.has_node("tool:doomed")
        assert "tool:doomed" in plugin.forgotten
        assert "tool:doomed" not in plugin.entities()
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_manifest_validation_flags_violations(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Startup validation flags a plugin exceeding its manifest."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()

    manifest = PluginManifest(
        allowed_read_paths=(allowed_dir,),
        allow_network=False,
    )
    plugin = FakeSensingPlugin(name="restricted_plugin", manifest=manifest)
    # Set an attribute pointing outside allowed_read_paths
    plugin.calendar_path = str(tmp_path / "secret" / "passwords.txt")
    # Set network access
    plugin.network_url = "https://unauthorized.example.com"

    host, _gm, store, bus = await _setup_host(tmp_path, fake_clock, plugin)
    try:
        violations = host.validate_manifests()
        assert len(violations) >= 2
        assert any("exceeds manifest allowed_read_paths" in v for v in violations)
        assert any("network access" in v for v in violations)

        # Health reflects violations
        h = host.health()
        assert h.ok is False
        assert "manifest violations" in h.detail
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
