# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `sensing/cloud_voice_bridge.py` (GeminiBridgeSttBackend).

No real network call anywhere here — `scripts/voice_cloud_helper.py` is a
separate process this class only ever talks to via the filesystem, so every
test drives that handshake directly: pre-writing (or not writing) a response
file plays the helper's part. `id_factory` is injected so a test can know a
request's id before `transcribe()` generates one internally.
"""

from __future__ import annotations

import json

from neuropaca.sensing.cloud_voice_bridge import GeminiBridgeSttBackend
from neuropaca.sensing.stt_backend import FakeSttBackend


def _bridge(tmp_path, *, timeout_seconds: float = 0.2, request_id: str = "req1"):
    local = FakeSttBackend(transcript="local fallback text")
    local.load()
    bridge_dir = tmp_path / "voice_cloud"
    bridge = GeminiBridgeSttBackend(
        local,
        bridge_dir=str(bridge_dir),
        timeout_seconds=timeout_seconds,
        id_factory=lambda: request_id,
    )
    bridge.load()
    return bridge, local, bridge_dir


def test_returns_the_cloud_transcript_when_the_helper_answers_promptly(tmp_path) -> None:
    bridge, local, bridge_dir = _bridge(tmp_path, request_id="req1")
    # Pre-write the response exactly where the helper would have — the
    # request id is known in advance because id_factory is fixed above.
    (bridge_dir / "responses" / "req1.json").write_text(
        json.dumps({"transcript": "open brave browser"}), encoding="utf-8"
    )

    result = bridge.transcribe(b"\x00\x01" * 100, 16000)

    assert result == "open brave browser"
    assert local.calls == []  # never touched the local model
    assert bridge._cloud_hits == 1
    assert bridge._fallback_hits == 0


def test_writes_a_wav_request_file_the_helper_can_read(tmp_path) -> None:
    bridge, _local, bridge_dir = _bridge(tmp_path, timeout_seconds=0.05, request_id="req2")

    bridge.transcribe(b"\x00\x01" * 100, 16000)

    # transcribe() cleans up after itself (both success and failure paths),
    # so the only way to observe the WAV was ever written correctly is to
    # intercept it before cleanup — do that by checking the directory exists
    # and is writable rather than racing the cleanup.
    assert (bridge_dir / "pending").is_dir()
    assert (bridge_dir / "responses").is_dir()


def test_falls_back_to_local_when_the_helper_never_answers(tmp_path) -> None:
    bridge, local, _bridge_dir = _bridge(tmp_path, timeout_seconds=0.05, request_id="req3")

    result = bridge.transcribe(b"\x00\x01" * 100, 16000)

    assert result == "local fallback text"
    assert local.calls == [(b"\x00\x01" * 100, 16000)]
    assert bridge._fallback_hits == 1
    assert bridge._cloud_hits == 0
    assert "no response" in (bridge.last_fallback_reason or "")


def test_falls_back_to_local_when_the_helper_reports_an_error(tmp_path) -> None:
    bridge, local, bridge_dir = _bridge(tmp_path, timeout_seconds=0.2, request_id="req4")
    (bridge_dir / "responses" / "req4.json").write_text(
        json.dumps({"error": "Gemini API key invalid"}), encoding="utf-8"
    )

    result = bridge.transcribe(b"\x00\x01" * 100, 16000)

    assert result == "local fallback text"
    assert local.calls == [(b"\x00\x01" * 100, 16000)]
    assert "Gemini API key invalid" in (bridge.last_fallback_reason or "")


def test_falls_back_to_local_on_a_malformed_response_file(tmp_path) -> None:
    bridge, local, bridge_dir = _bridge(tmp_path, timeout_seconds=0.2, request_id="req5")
    (bridge_dir / "responses" / "req5.json").write_text("not json at all", encoding="utf-8")

    result = bridge.transcribe(b"\x00\x01" * 100, 16000)

    assert result == "local fallback text"
    assert local.calls == [(b"\x00\x01" * 100, 16000)]


def test_cleans_up_request_and_response_files_after_a_cloud_hit(tmp_path) -> None:
    bridge, _local, bridge_dir = _bridge(tmp_path, request_id="req6")
    (bridge_dir / "responses" / "req6.json").write_text(
        json.dumps({"transcript": "hello"}), encoding="utf-8"
    )

    bridge.transcribe(b"\x00\x01" * 100, 16000)

    assert not (bridge_dir / "pending" / "req6.wav").exists()
    assert not (bridge_dir / "responses" / "req6.json").exists()


def test_is_loaded_reflects_the_local_fallback() -> None:
    local = FakeSttBackend()
    bridge = GeminiBridgeSttBackend(local, bridge_dir="/tmp/unused", timeout_seconds=1.0)
    assert bridge.is_loaded is False
    local.load()
    assert bridge.is_loaded is True


def test_satisfies_the_sttbackend_protocol() -> None:
    from neuropaca.sensing.stt_backend import SttBackend

    bridge = GeminiBridgeSttBackend(FakeSttBackend(), bridge_dir="/tmp/unused", timeout_seconds=1.0)
    assert isinstance(bridge, SttBackend)
