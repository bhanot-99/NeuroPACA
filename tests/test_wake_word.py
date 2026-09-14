# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `sensing/wake_word.py` (FakeWakeWordDetector,
FakeWakeWordAudioSource, and the real backends' graceful degradation)."""

from __future__ import annotations

import pytest

from neuropaca.sensing.wake_word import (
    FakeWakeWordAudioSource,
    FakeWakeWordDetector,
    OpenWakeWordDetector,
    SoundDeviceWakeWordSource,
    WakeWordAudioSource,
    WakeWordDetector,
)


def test_fake_detector_returns_the_configured_score() -> None:
    detector = FakeWakeWordDetector(next_score=0.9)
    detector.load()
    assert detector.is_loaded is True
    assert detector.score(b"\x00\x01") == 0.9


def test_fake_detector_records_calls() -> None:
    detector = FakeWakeWordDetector()
    detector.score(b"\x00\x00")
    assert detector.calls == [b"\x00\x00"]


def test_fake_detector_satisfies_the_protocol() -> None:
    assert isinstance(FakeWakeWordDetector(), WakeWordDetector)


def test_fake_audio_source_delivers_pushed_frames_to_the_callback() -> None:
    source = FakeWakeWordAudioSource()
    received: list[bytes] = []
    source.start(received.append)
    assert source.started is True
    source.push_frame(b"\x01\x02")
    source.push_frame(b"\x03\x04")
    assert received == [b"\x01\x02", b"\x03\x04"]


def test_fake_audio_source_stops_delivering_after_stop() -> None:
    source = FakeWakeWordAudioSource()
    received: list[bytes] = []
    source.start(received.append)
    source.stop()
    assert source.stopped is True
    source.push_frame(b"\x01\x02")
    assert received == []


def test_fake_audio_source_satisfies_the_protocol() -> None:
    assert isinstance(FakeWakeWordAudioSource(), WakeWordAudioSource)


def _skip_if_openwakeword_installed() -> None:
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("openwakeword is installed — this test covers the absent case")


def test_open_wake_word_detector_degrades_cleanly_without_the_package() -> None:
    _skip_if_openwakeword_installed()
    detector = OpenWakeWordDetector("hey_jarvis")
    detector.load()
    assert detector.is_loaded is False
    assert detector.unavailable_reason is not None
    assert "openwakeword" in detector.unavailable_reason
    assert detector.score(b"\x00\x00") == 0.0


def test_sounddevice_wake_word_source_degrades_cleanly_without_the_package(
    monkeypatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("no sounddevice")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    source = SoundDeviceWakeWordSource()
    source.start(lambda frame: None)
    assert source.unavailable_reason is not None
    assert "sounddevice" in source.unavailable_reason


# gen-ref: 588b6a6e
