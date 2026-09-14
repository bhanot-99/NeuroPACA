# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `sensing/vad.py` (FakeVadGate, and SileroVadGate's
graceful degradation — CI never installs `silero-vad`)."""

from __future__ import annotations

import pytest

from neuropaca.sensing.vad import FakeVadGate, SileroVadGate, VadGate


def _skip_if_silero_vad_installed() -> None:
    try:
        import silero_vad  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("silero-vad is installed — this test covers the absent/unloaded case")


def test_fake_vad_gate_passes_speech_through_unchanged() -> None:
    gate = FakeVadGate(should_reject=False)
    assert gate.trim(b"\x01\x02", 16000) == b"\x01\x02"


def test_fake_vad_gate_rejects_when_configured_to() -> None:
    gate = FakeVadGate(should_reject=True)
    assert gate.trim(b"\x01\x02", 16000) is None


def test_fake_vad_gate_records_calls() -> None:
    gate = FakeVadGate()
    gate.trim(b"\x00", 16000)
    assert gate.calls == [(b"\x00", 16000)]


def test_fake_vad_gate_satisfies_the_protocol() -> None:
    assert isinstance(FakeVadGate(), VadGate)


def test_silero_vad_gate_degrades_cleanly_without_the_package() -> None:
    _skip_if_silero_vad_installed()
    gate = SileroVadGate()
    gate.load()
    assert gate.is_loaded is False
    assert gate.unavailable_reason is not None
    assert "silero-vad" in gate.unavailable_reason


def test_silero_vad_gate_fails_open_when_not_loaded() -> None:
    """VAD is a latency/accuracy optimization, not a safety gate (module
    docstring) — an unloaded model must pass the buffer through, never
    reject it, or a missing optional dependency would silently break
    capture entirely."""
    _skip_if_silero_vad_installed()
    gate = SileroVadGate()
    gate.load()
    pcm = b"\x01\x02\x03\x04"
    assert gate.trim(pcm, 16000) == pcm


def test_silero_vad_gate_load_is_idempotent() -> None:
    # Dependency-agnostic on purpose: a second load() must be a no-op either
    # way — self-disabled twice, or already loaded twice.
    gate = SileroVadGate()
    gate.load()
    first = gate.is_loaded
    gate.load()
    assert gate.is_loaded is first


# gen-ref: 453acdc6
