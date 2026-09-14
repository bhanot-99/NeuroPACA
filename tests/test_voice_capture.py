# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `sensing/voice_capture.py` (VoiceCaptureModule).

Entirely against `FakeAudioSource`/`FakeVadGate`/`FakeSttBackend` — no real
microphone, VAD model, or STT model needed, mirroring how A6.1/A6.2 test the
full intent/command pipeline against `FakeInferenceBackend`.
"""

from __future__ import annotations

import json
from pathlib import Path

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.sensing.stt_backend import FakeSttBackend
from neuropaca.sensing.vad import FakeVadGate
from neuropaca.sensing.voice_capture import FakeAudioSource, VoiceCaptureModule


def _read_last_utterance(path: Path) -> str:
    lines = path.read_text("utf-8").splitlines()
    return json.loads(lines[-1])["text"]


async def test_ptt_session_writes_the_transcript_to_voice_utterances_path(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    await bus.start()
    utterances_path = tmp_path / "utterances.jsonl"
    cfg = Config(inference_backend="fake", voice_utterances_path=str(utterances_path))
    audio = FakeAudioSource(pcm=b"\x01\x02\x03\x04")
    vad = FakeVadGate(should_reject=False)
    stt = FakeSttBackend(transcript="open Brave")

    module = VoiceCaptureModule(bus, cfg, audio, vad, stt)
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        assert audio.started is True

        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert audio.stopped is True
        assert vad.calls == [(b"\x01\x02\x03\x04", 16000)]
        assert stt.calls == [(b"\x01\x02\x03\x04", 16000)]
        assert _read_last_utterance(utterances_path) == "open Brave"
    finally:
        await module.stop()
        await bus.stop()


async def test_vad_rejection_never_reaches_stt_or_the_file(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    utterances_path = tmp_path / "utterances.jsonl"
    cfg = Config(inference_backend="fake", voice_utterances_path=str(utterances_path))
    stt = FakeSttBackend(transcript="should never be seen")
    module = VoiceCaptureModule(
        bus, cfg, FakeAudioSource(pcm=b"\x00"), FakeVadGate(should_reject=True), stt
    )
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert stt.calls == []
        assert not utterances_path.exists()
        assert module._rejected_silence == 1
    finally:
        await module.stop()
        await bus.stop()


async def test_vad_disabled_skips_the_gate_entirely(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    utterances_path = tmp_path / "utterances.jsonl"
    cfg = Config(
        inference_backend="fake",
        voice_utterances_path=str(utterances_path),
        voice_vad_enabled=False,
    )
    vad = FakeVadGate(should_reject=True)  # would reject everything if consulted
    stt = FakeSttBackend(transcript="hello")
    module = VoiceCaptureModule(bus, cfg, FakeAudioSource(pcm=b"\x00"), vad, stt)
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert vad.calls == []
        assert _read_last_utterance(utterances_path) == "hello"
    finally:
        await module.stop()
        await bus.stop()


async def test_empty_transcript_is_never_appended(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    utterances_path = tmp_path / "utterances.jsonl"
    cfg = Config(inference_backend="fake", voice_utterances_path=str(utterances_path))
    module = VoiceCaptureModule(
        bus, cfg, FakeAudioSource(pcm=b"\x00"), FakeVadGate(), FakeSttBackend(transcript="   ")
    )
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert not utterances_path.exists()
        assert module._empty_transcripts == 1
    finally:
        await module.stop()
        await bus.stop()


async def test_a_stray_start_while_already_active_is_ignored(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake", voice_utterances_path=str(tmp_path / "utterances.jsonl"))
    module = VoiceCaptureModule(
        bus, cfg, FakeAudioSource(pcm=b"\x00"), FakeVadGate(), FakeSttBackend()
    )
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()

        assert module._sessions == 1
    finally:
        await module.stop()
        await bus.stop()


async def test_a_stray_stop_with_no_active_session_is_ignored(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    cfg = Config(inference_backend="fake", voice_utterances_path=str(tmp_path / "utterances.jsonl"))
    audio = FakeAudioSource(pcm=b"\x00")
    module = VoiceCaptureModule(bus, cfg, audio, FakeVadGate(), FakeSttBackend())
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert audio.stopped is False
    finally:
        await module.stop()
        await bus.stop()


async def test_a_session_that_never_gets_stopped_is_force_finished_by_the_timeout(
    tmp_path: Path,
) -> None:
    """Regression: `voice_ptt_max_seconds` used to be validated but never
    enforced — a lost `_STOPPED` left the mic capturing (and the buffer
    growing) forever."""
    bus = EventBus()
    await bus.start()
    utterances_path = tmp_path / "utterances.jsonl"
    cfg = Config(
        inference_backend="fake",
        voice_utterances_path=str(utterances_path),
        voice_ptt_max_seconds=0.01,
    )
    audio = FakeAudioSource(pcm=b"\x01\x02\x03\x04")
    module = VoiceCaptureModule(
        bus, cfg, audio, FakeVadGate(), FakeSttBackend(transcript="timed out")
    )
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        assert audio.started is True

        # No VOICE_PTT_STOPPED ever arrives — wait for the internal timeout
        # task itself rather than a real-time sleep (deterministic, no flake).
        timeout_task = module._timeout_task
        assert timeout_task is not None
        await timeout_task

        assert audio.stopped is True
        assert module._timed_out == 1
        assert module._session_active is False
        assert _read_last_utterance(utterances_path) == "timed out"
    finally:
        await module.stop()
        await bus.stop()


async def test_a_normal_stop_before_the_timeout_cancels_it(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    cfg = Config(
        inference_backend="fake",
        voice_utterances_path=str(tmp_path / "utterances.jsonl"),
        voice_ptt_max_seconds=30.0,  # long enough that it would never fire in this test
    )
    module = VoiceCaptureModule(
        bus, cfg, FakeAudioSource(pcm=b"\x00"), FakeVadGate(), FakeSttBackend(transcript="hi")
    )
    await module.initialize()
    await module.start()

    try:
        bus.publish(Event(event_type=EventType.VOICE_PTT_STARTED, source="test", payload={}))
        await bus.join()
        bus.publish(Event(event_type=EventType.VOICE_PTT_STOPPED, source="test", payload={}))
        await bus.join()

        assert module._timed_out == 0
        assert module._timeout_task is None
    finally:
        await module.stop()
        await bus.stop()


def test_health_reports_ok_while_running() -> None:
    bus = EventBus()
    cfg = Config(inference_backend="fake")
    module = VoiceCaptureModule(bus, cfg, FakeAudioSource(), FakeVadGate(), FakeSttBackend())
    module.is_running = True
    health = module.health()
    assert health.ok is True
    assert "0 sessions" in health.detail


def test_build_modules_wires_voice_capture_and_activation(tmp_path: Path) -> None:
    from neuropaca.core.bitnet_runtime import BitNetRuntime
    from neuropaca.core.graph_memory import GraphMemory
    from neuropaca.core.inference import FakeInferenceBackend
    from neuropaca.interface.activation import VoiceActivationModule
    from neuropaca.orchestration.modules import build_modules

    bus = EventBus()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    backend = FakeInferenceBackend()
    runtime = BitNetRuntime(backend, backend)

    try:
        # Disabled by default
        cfg_off = Config(inference_backend="fake")
        modules_off = build_modules(cfg_off, bus, gm, runtime)
        assert not any(isinstance(m, VoiceCaptureModule) for m in modules_off)
        assert not any(isinstance(m, VoiceActivationModule) for m in modules_off)

        # Enabled
        cfg_on = Config(
            inference_backend="fake",
            voice_enabled=True,
            voice_speech_enabled=True,
            voice_utterances_path=str(tmp_path / "utterances.jsonl"),
        )
        modules_on = build_modules(cfg_on, bus, gm, runtime)
        names = [m.name for m in modules_on]
        assert "voice_capture" in names
        assert "voice_activation" in names
        # VoiceCaptureModule must already be subscribed before
        # VoiceActivationModule can publish anything (module docstring).
        idx_capture = names.index("voice_capture")
        idx_activation = names.index("voice_activation")
        assert idx_activation == idx_capture + 1
    finally:
        GraphMemory._reset_for_tests()


# gen-ref: 996103d8
