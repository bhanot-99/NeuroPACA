# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for VoicePlugin (A6.1 · domain:voice).

Verifies the JSONL utterance format, `since` watermark filtering, malformed-
line tolerance, the side-effect-free plugin contract, PluginHost integration
(including the new domain-hub auto-seed for a brand-new domain), and that
`forget()` actually removes the raw line, not just derived records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from plugins.voice.voice_plugin import VoicePlugin, append_utterance

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.sensing.plugin_host import PluginHost

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 14, 12, 0, tzinfo=UTC))


async def test_items_parses_appended_utterances(tmp_path: Path) -> None:
    path = tmp_path / "utterances.jsonl"
    ts = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    append_utterance(path, "remind me to email Maya back", ts=ts)

    plugin = VoicePlugin(path)
    desc = plugin.describe()
    assert desc.name == "voice"
    assert desc.domain_hub == "domain:voice"
    assert desc.span_kind == EpisodeKind.VOICE_UTTERANCE_SPAN
    assert desc.manifest.allowed_episode_kinds == (
        EpisodeKind.VOICE_UTTERANCE_SPAN,
        EpisodeKind.PLUGIN_FACT,
    )

    items = await plugin.items(_EPOCH)
    assert len(items) == 1
    item = items[0]
    assert item.label == "remind me to email Maya back"
    assert item.span == (ts, ts)
    assert item.span_kind == EpisodeKind.VOICE_UTTERANCE_SPAN
    assert item.fact == (
        EpisodeKind.PLUGIN_FACT,
        "remind me to email Maya back",
        {"source": "typed"},
    )
    assert (item.entity_id, "domain:voice", RelationType.PART_OF) in item.edges
    assert len(item.events) == 1
    assert item.events[0].event_type == EventType.VOICE_UTTERANCE_CAPTURED
    assert item.events[0].payload == {"entity_id": item.entity_id, "text": item.label}


async def test_since_watermark_filters_old_utterances(tmp_path: Path) -> None:
    path = tmp_path / "utterances.jsonl"
    older = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 14, 11, 0, tzinfo=UTC)
    append_utterance(path, "older utterance", ts=older)
    append_utterance(path, "newer utterance", ts=newer)

    plugin = VoicePlugin(path)
    watermark = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    items = await plugin.items(watermark)

    assert len(items) == 1
    assert items[0].label == "newer utterance"


async def test_malformed_lines_are_skipped_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "utterances.jsonl"
    path.write_text(
        "\n".join(
            [
                "not even json",
                '{"text": "no timestamp here"}',
                '{"ts": "2026-09-14T10:00:00+00:00"}',  # no text
                '{"text": "", "ts": "2026-09-14T10:00:00+00:00"}',  # blank text
                '{"text": "a good line", "ts": "2026-09-14T10:00:00+00:00"}',
            ]
        ),
        encoding="utf-8",
    )

    plugin = VoicePlugin(path)
    items = await plugin.items(_EPOCH)

    assert len(items) == 1
    assert items[0].label == "a good line"


async def test_entity_id_is_stable_across_reads(tmp_path: Path) -> None:
    """Content-addressed ids: re-reading the same file (a restart) must not
    mint new graph nodes for utterances already seen."""
    path = tmp_path / "utterances.jsonl"
    append_utterance(path, "same text", ts=datetime(2026, 9, 14, 10, 0, tzinfo=UTC))

    first = await VoicePlugin(path).items(_EPOCH)
    second = await VoicePlugin(path).items(_EPOCH)

    assert first[0].entity_id == second[0].entity_id


async def test_owns_entity_and_prefix(tmp_path: Path) -> None:
    plugin = VoicePlugin(tmp_path / "utterances.jsonl")
    assert plugin.owns_entity("utterance:deadbeef")
    assert not plugin.owns_entity("event:something-else")


async def test_plugin_host_integration_seeds_new_hub_and_forget_scrubs_file(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    path = tmp_path / "utterances.jsonl"
    ts = fake_clock.now()
    append_utterance(path, "open spotify", ts=ts)
    append_utterance(path, "unrelated later line", ts=ts)

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    captured_events: list[str] = []

    async def _capture(ev: object) -> None:
        captured_events.append(ev.payload["text"])  # type: ignore[attr-defined]

    bus.subscribe(EventType.VOICE_UTTERANCE_CAPTURED, _capture)

    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    # `load()` on a genuinely empty graph seeds every current hub, including
    # "domain:voice" — so to exercise the fix (an *existing*, already-
    # populated graph gaining a domain it predates), delete it back out
    # first. This is exactly PluginHost.initialize()'s new job: S1-S4 never
    # needed it because they all reused an existing hub; voice is the first
    # plugin to introduce one nothing has routed to yet.
    await gm.delete_node("domain:voice")
    assert not gm.has_node("domain:voice")

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    plugin = VoicePlugin(path)
    host = PluginHost(bus, cfg, gm, store, plugins=[plugin], clock=fake_clock)
    await host.initialize()

    try:
        assert gm.has_node("domain:voice")

        facts = await host.poll_tick()
        assert facts == 2
        await store.flush()
        await bus.join()  # let the queued VOICE_UTTERANCE_CAPTURED events dispatch
        assert sorted(captured_events) == ["open spotify", "unrelated later line"]

        first_entity = next(iter(plugin.entities()))
        node = gm.get_node(first_entity)
        assert node is not None
        assert node.node_type == NodeType.CONCEPT
        assert any(
            e.target_id == "domain:voice" and e.relation == RelationType.PART_OF
            for e in gm.get_edges(first_entity)
        )

        entity_to_forget = next(e for e in plugin.entities())
        removed = await host.forget(entity_to_forget)
        assert removed >= 1
        assert not gm.has_node(entity_to_forget)

        # The raw line actually left the file — not just the graph/episode
        # records derived from it (rules.md §8).
        remaining_lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining_lines) == 1
    finally:
        await host.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_voice_plugin_resolves_app_identity(tmp_path: Path) -> None:
    from neuropaca.diagnosis.app_identity import AppIdentity

    path = tmp_path / "utterances.jsonl"
    ts = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    append_utterance(path, "open Brave browser", ts=ts)
    append_utterance(path, "what is the weather today", ts=ts)

    identity = AppIdentity.from_dict({
        "alias": {"brave-browser": "brave", "brave": "brave"},
        "non_app": [],
    })
    plugin = VoicePlugin(path, identity=identity)
    items = await plugin.items(_EPOCH)

    assert len(items) == 2
    app_item = next(it for it in items if it.entity_id.startswith("app:"))
    assert app_item.entity_id == "app:brave"
    assert app_item.node_type == NodeType.APP
    assert app_item.label == "Brave"
    assert (app_item.entity_id, "domain:voice", RelationType.PART_OF) in app_item.edges

    concept_item = next(it for it in items if it.entity_id.startswith("utterance:"))
    assert concept_item.node_type == NodeType.CONCEPT
    assert concept_item.label == "what is the weather today"
