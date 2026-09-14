# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.2/rules.md §5.2 · `interface/voice_confirmation.py`
(VoiceConfirmationBridge) — the voice-based answerer for dangerous-action
confirmations.

Drives the real ACTION_CONFIRMATION_REQUEST/VOICE_UTTERANCE_CAPTURED/
ACTION_CONFIRMATION_RESPONSE event contract directly — this module never
imports `ConfirmationBroker` (rules.md §0), so neither do these tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.interface.voice_confirmation import VoiceConfirmationBridge


def _request_event(request_id: str = "req1", summary: str = "open Brave Web Browser") -> Event:
    return Event(
        event_type=EventType.ACTION_CONFIRMATION_REQUEST,
        source="action",
        payload={
            "request_id": request_id,
            "action": "open_app",
            "tier": "dangerous",
            "summary": summary,
            "reason": "voice command",
            "requested_at": datetime.now(UTC).isoformat(),
        },
    )


def _utterance_event(text: str, entity_id: str = "utterance:1") -> Event:
    return Event(
        event_type=EventType.VOICE_UTTERANCE_CAPTURED,
        source="voice",
        payload={"entity_id": entity_id, "text": text},
    )


async def _setup(timeout_seconds: float = 60.0) -> tuple[VoiceConfirmationBridge, EventBus]:
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake", action_confirmation_timeout_seconds=timeout_seconds)
    module = VoiceConfirmationBridge(bus, cfg)
    await module.initialize()
    await module.start()
    return module, bus


async def test_a_literal_yes_approves_the_pending_request() -> None:
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()
        bus.publish(_utterance_event("yes"))
        await bus.join()

        assert len(responses) == 1
        assert responses[0].payload == {"request_id": "req1", "approved": True}
    finally:
        await module.stop()
        await bus.stop()


async def test_a_literal_no_denies_the_pending_request() -> None:
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()
        bus.publish(_utterance_event("no"))
        await bus.join()

        assert len(responses) == 1
        assert responses[0].payload == {"request_id": "req1", "approved": False}
    finally:
        await module.stop()
        await bus.stop()


async def test_case_and_punctuation_insensitive_but_still_exact() -> None:
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()
        bus.publish(_utterance_event("  Yes!  "))
        await bus.join()

        assert len(responses) == 1
        assert responses[0].payload["approved"] is True
    finally:
        await module.stop()
        await bus.stop()


async def test_a_sentence_merely_containing_yes_does_not_match() -> None:
    """rules.md's own risk table: strict/literal, never loose — "yes, confirm
    the following meeting" must NOT be treated as a confirmation answer."""
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()
        bus.publish(_utterance_event("yes, confirm the following meeting"))
        await bus.join()

        assert responses == []
    finally:
        await module.stop()
        await bus.stop()


async def test_an_utterance_with_no_pending_request_is_ignored() -> None:
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_utterance_event("yes"))
        await bus.join()

        assert responses == []
    finally:
        await module.stop()
        await bus.stop()


async def test_an_utterance_after_the_timeout_no_longer_resolves_it() -> None:
    """Fail-closed, unchanged (module docstring): this module's local
    tracking must not resurrect a request past its own timeout window —
    ConfirmationBroker's real timeout is the one source of truth."""
    module, bus = await _setup(timeout_seconds=0.05)
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()

        import asyncio

        await asyncio.sleep(0.1)  # past the 0.05s window

        bus.publish(_utterance_event("yes"))
        await bus.join()

        assert responses == []
    finally:
        await module.stop()
        await bus.stop()


async def test_multiple_pending_requests_resolve_oldest_first() -> None:
    module, bus = await _setup()
    responses: list[Event] = []
    bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, responses.append)
    try:
        bus.publish(_request_event(request_id="req_first"))
        await bus.join()
        bus.publish(_request_event(request_id="req_second"))
        await bus.join()

        bus.publish(_utterance_event("yes"))
        await bus.join()

        assert len(responses) == 1
        assert responses[0].payload["request_id"] == "req_first"
    finally:
        await module.stop()
        await bus.stop()


async def test_health_reports_counters() -> None:
    module, bus = await _setup()
    try:
        bus.publish(_request_event(request_id="req1"))
        await bus.join()
        bus.publish(_utterance_event("yes"))
        await bus.join()

        health = module.health()
        assert health.ok is True
        assert "1 prompted" in health.detail
        assert "1 approved" in health.detail
    finally:
        await module.stop()
        await bus.stop()


def test_build_modules_wires_voice_confirmation_when_speech_enabled(tmp_path) -> None:
    from neuropaca.core.bitnet_runtime import BitNetRuntime
    from neuropaca.core.graph_memory import GraphMemory
    from neuropaca.core.inference import FakeInferenceBackend
    from neuropaca.orchestration.modules import build_modules

    bus = EventBus()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    backend = FakeInferenceBackend()
    runtime = BitNetRuntime(backend, backend)

    try:
        cfg_off = Config(inference_backend="fake")
        modules_off = build_modules(cfg_off, bus, gm, runtime)
        assert not any(isinstance(m, VoiceConfirmationBridge) for m in modules_off)

        cfg_on = Config(
            inference_backend="fake",
            voice_enabled=True,
            voice_speech_enabled=True,
            voice_utterances_path=str(tmp_path / "utterances.jsonl"),
        )
        modules_on = build_modules(cfg_on, bus, gm, runtime)
        assert any(isinstance(m, VoiceConfirmationBridge) for m in modules_on)
    finally:
        GraphMemory._reset_for_tests()
