# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.1's voice-intent grammar/prompt/parse (learning/prompts.py,
learning/voice_intent.py) and `VoiceIntentParser` (learning/voice_intent_parser.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import FakeInferenceBackend
from neuropaca.core.models import Event, Node
from neuropaca.learning.prompts import (
    build_voice_intent_grammar,
    build_voice_intent_prompt,
    parse_voice_intent,
)
from neuropaca.learning.voice_intent import VOICE_INTENT_CATEGORIES, VoiceIntent
from neuropaca.learning.voice_intent_parser import VoiceIntentParser


# --------------------------------------------------------------------------- grammar
def test_build_voice_intent_grammar_shape() -> None:
    grammar = build_voice_intent_grammar(["n1", "n2"])
    assert '\\"n1\\"' in grammar
    assert '\\"n2\\"' in grammar
    assert "voice_intent" in grammar
    for category in VOICE_INTENT_CATEGORIES:
        assert f'\\"{category}\\"' in grammar


def test_build_voice_intent_grammar_rejects_empty_aliases() -> None:
    with pytest.raises(ValueError, match="at least one alias"):
        build_voice_intent_grammar([])


def test_build_voice_intent_grammar_rejects_bad_alias_format() -> None:
    with pytest.raises(ValueError, match="not a local alias"):
        build_voice_intent_grammar(["not-an-alias"])


def test_build_voice_intent_grammar_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate aliases"):
        build_voice_intent_grammar(["n1", "n1"])


def test_build_voice_intent_prompt_contains_utterance_and_facts() -> None:
    node = Node(id="app:spotify", node_type=NodeType.APP, label="Spotify")
    prompt = build_voice_intent_prompt("open spotify", [("n1", node)])
    assert "open spotify" in prompt
    assert "spotify" in prompt.lower()
    assert "Answer:" in prompt


# --------------------------------------------------------------------------- parse
def test_parse_voice_intent_valid_with_cited_node() -> None:
    alias_to_id = {"n1": "app:spotify"}
    raw = '{"cited_node_id": "n1", "voice_intent": "action_request"}'
    intent = parse_voice_intent(raw, alias_to_id, raw_text="open spotify")
    assert intent == VoiceIntent(
        category="action_request", cited_node_id="app:spotify", raw_text="open spotify"
    )


def test_parse_voice_intent_valid_with_null_cited_node() -> None:
    raw = '{"cited_node_id": null, "voice_intent": "reminder"}'
    intent = parse_voice_intent(raw, {}, raw_text="remind me later")
    assert intent is not None
    assert intent.category == "reminder"
    assert intent.cited_node_id is None


def test_parse_voice_intent_rejects_unknown_category() -> None:
    raw = '{"cited_node_id": null, "voice_intent": "not_a_real_category"}'
    assert parse_voice_intent(raw, {}, raw_text="x") is None


def test_parse_voice_intent_rejects_out_of_vocab_alias() -> None:
    raw = '{"cited_node_id": "n9", "voice_intent": "other"}'
    assert parse_voice_intent(raw, {"n1": "app:spotify"}, raw_text="x") is None


def test_parse_voice_intent_rejects_malformed_json() -> None:
    assert parse_voice_intent("not json at all", {}, raw_text="x") is None


# --------------------------------------------------------------------------- parser module
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def test_voice_intent_parser_classifies_and_enriches_fact(tmp_path: Path) -> None:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("app:spotify", NodeType.APP, attributes={"label": "Spotify"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    runtime = BitNetRuntime(FakeInferenceBackend(), FakeInferenceBackend())

    parser = VoiceIntentParser(bus, cfg, gm, runtime, episode_store=store)
    await parser.initialize()
    await parser.start()

    entity_id = "utterance:deadbeef"
    text = "open spotify"
    try:
        bus.publish(
            Event(
                event_type=EventType.VOICE_UTTERANCE_CAPTURED,
                source="voice",
                payload={"entity_id": entity_id, "text": text},
            )
        )
        await bus.join()
        await store.flush()

        assert parser.health().detail.startswith("interactive model loaded")
        records = await store.at(datetime.now(UTC))
        matching = [r for r in records if r.subject == entity_id]
        assert len(matching) == 1
        assert matching[0].kind == str(EpisodeKind.PLUGIN_FACT)
        assert matching[0].object == text
        assert matching[0].attrs["voice_intent"] == "other"  # FakeInferenceBackend's default
        assert matching[0].source == "voice_intent"
    finally:
        await parser.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_voice_intent_parser_drops_when_graph_has_no_candidate_nodes(
    tmp_path: Path,
) -> None:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()  # a fresh graph has only hub nodes, no non-hub candidates

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    runtime = BitNetRuntime(FakeInferenceBackend(), FakeInferenceBackend())
    parser = VoiceIntentParser(bus, cfg, gm, runtime, episode_store=store)
    await parser.initialize()
    await parser.start()

    try:
        await parser.on_utterance_event(
            Event(
                event_type=EventType.VOICE_UTTERANCE_CAPTURED,
                payload={"entity_id": "utterance:x", "text": "hello"},
            )
        )
        await store.flush()
        assert parser._classified == 0
        assert parser._drops["no_nodes"] == 1
        records = await store.at(datetime.now(UTC))
        assert not any(r.subject == "utterance:x" for r in records)
    finally:
        await parser.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_voice_intent_parser_drops_when_no_interactive_backend(tmp_path: Path) -> None:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node("app:spotify", NodeType.APP, attributes={"label": "Spotify"})

    # No interactive backend configured — the dormant-since-CLI-removal case.
    runtime = BitNetRuntime(FakeInferenceBackend())
    parser = VoiceIntentParser(bus, cfg, gm, runtime)
    await parser.initialize()
    await parser.start()

    try:
        await parser.on_utterance_event(
            Event(
                event_type=EventType.VOICE_UTTERANCE_CAPTURED,
                payload={"entity_id": "utterance:x", "text": "hello"},
            )
        )
        assert parser._drops["model"] == 1
    finally:
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


# gen-ref: dc3927e4
