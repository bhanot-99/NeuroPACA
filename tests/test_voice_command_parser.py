# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.2 Step 6 · VoiceCommandParser (learning/voice_command_parser.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import FakeInferenceBackend
from neuropaca.core.models import Event
from neuropaca.learning.voice_command_parser import VoiceCommandParser

_MOCK_APPS = {
    "Brave Browser": "brave-browser",
    "Google Chrome": "google-chrome",
    "Calculator": "gnome-calculator",
    "Files": "nautilus",
    "Firefox Web Browser": "firefox",
}


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def fake_runtime() -> BitNetRuntime:
    backend = FakeInferenceBackend()
    return BitNetRuntime(backend, backend)


async def test_voice_command_parser_ignores_non_action_request(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={"entity_id": "e1", "text": "open Chrome", "category": "reminder"},
            )
        )
        await bus.join()
        assert len(proposals) == 0
        assert parser._drops["category"] == 1
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_tier0_open_app(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={"entity_id": "e1", "text": "open Chrome", "category": "action_request"},
            )
        )
        await bus.join()

        assert len(proposals) == 1
        assert proposals[0].payload["action_type"] == "open_app"
        assert proposals[0].payload["kwargs"]["app_name"] == "Google Chrome"
        assert proposals[0].payload["kwargs"]["launch_command"] == "google-chrome"
        assert parser._tier0_hits == 1
        assert parser._tier1_hits == 0
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_tier0_volume_and_brightness(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        # Volume
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={
                    "entity_id": "e1",
                    "text": "turn up the volume",
                    "category": "action_request",
                },
            )
        )
        await bus.join()
        assert len(proposals) == 1
        assert proposals[0].payload["action_type"] == "adjust_volume"
        assert proposals[0].payload["kwargs"]["direction"] == "increase"

        # Brightness
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={
                    "entity_id": "e2",
                    "text": "decrease the brightness",
                    "category": "action_request",
                },
            )
        )
        await bus.join()
        assert len(proposals) == 2
        assert proposals[1].payload["action_type"] == "adjust_brightness"
        assert proposals[1].payload["kwargs"]["direction"] == "decrease"
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_tier1_model_fallback(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        # Sentence that does not match Tier 0 regex, but contains "open" so
        # FakeInferenceBackend extracts it
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={
                    "entity_id": "e1",
                    "text": "can you please open Calculator",
                    "category": "action_request",
                },
            )
        )
        await bus.join()

        assert len(proposals) == 1
        assert proposals[0].payload["action_type"] == "open_app"
        assert proposals[0].payload["kwargs"]["app_name"] == "Calculator"
        assert parser._tier1_hits == 1
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_tier1_model_fallback_non_open_action(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    """Regression: `_fake_voice_command` used to scan the *whole* prompt,
    including the fixed system instructions ("select an action from: open,
    close, ..."), so it always resolved to "open" no matter the utterance.
    A Tier-1 fallback for any other action was untested and, on the real
    model, would have been misclassified by anything relying on this fake."""
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        # Does not match the Tier 0 volume/brightness regex (wrong phrasing),
        # but contains "increase" and ends in "brightness" for the fake
        # backend's span-pointer stand-in to pick up.
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={
                    "entity_id": "e1",
                    "text": "hey machine please increase the brightness",
                    "category": "action_request",
                },
            )
        )
        await bus.join()

        assert len(proposals) == 1
        assert proposals[0].payload["action_type"] == "adjust_brightness"
        assert proposals[0].payload["kwargs"]["direction"] == "increase"
        assert parser._tier1_hits == 1
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_ambiguity_surfaces_notification(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    proposals: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL, proposals.append)

    try:
        # 1. Zero matches
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={
                    "entity_id": "e1",
                    "text": "open nonexistentapp",
                    "category": "action_request",
                },
            )
        )
        await bus.join()
        assert len(proposals) == 1
        assert proposals[0].payload["action_type"] == "notification"
        assert "found 0 matches, did nothing" in proposals[0].payload["kwargs"]["text"]

        # 2. Multiple matches (e.g. "Browser" matches Brave Browser and Firefox Web Browser)
        bus.publish(
            Event(
                event_type=EventType.VOICE_INTENT_CLASSIFIED,
                source="voice_intent",
                payload={"entity_id": "e2", "text": "open Browser", "category": "action_request"},
            )
        )
        await bus.join()
        assert len(proposals) == 2
        assert proposals[1].payload["action_type"] == "notification"
        assert "found 2 matches" in proposals[1].payload["kwargs"]["text"]

        assert parser._ambiguous == 2
    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL, proposals.append)
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


async def test_voice_command_parser_health(
    bus: EventBus, fake_runtime: BitNetRuntime, tmp_path: Path
) -> None:
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    cfg = Config(inference_backend="fake")

    parser = VoiceCommandParser(bus, cfg, gm, fake_runtime, app_registry=_MOCK_APPS)
    await parser.initialize()
    await parser.start()

    try:
        h = parser.health()
        assert h.ok is True
        assert "0 parsed" in h.detail
    finally:
        await parser.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()


def test_build_modules_wires_voice_command_parser(tmp_path: Path) -> None:
    from neuropaca.orchestration.modules import build_modules

    bus = EventBus()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    backend = FakeInferenceBackend()
    runtime = BitNetRuntime(backend, backend)

    # Disabled by default
    cfg_off = Config(inference_backend="fake")
    modules_off = build_modules(cfg_off, bus, gm, runtime)
    assert not any(isinstance(m, VoiceCommandParser) for m in modules_off)

    # Enabled
    cfg_on = Config(
        inference_backend="fake",
        voice_enabled=True,
        voice_commands_enabled=True,
        voice_utterances_path=str(tmp_path / "utterances.jsonl"),
    )
    modules_on = build_modules(cfg_on, bus, gm, runtime)
    names = [m.name for m in modules_on]
    assert "voice_command" in names
    # Ensure VoiceCommandParser is right after VoiceIntentParser
    idx_intent = names.index("voice_intent")
    idx_cmd = names.index("voice_command")
    assert idx_cmd == idx_intent + 1
    GraphMemory._reset_for_tests()
