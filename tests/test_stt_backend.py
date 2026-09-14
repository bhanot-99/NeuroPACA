# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `sensing/stt_backend.py` (FakeSttBackend, and
FasterWhisperBackend's graceful degradation when the optional dependency
is not installed — CI never installs it, same as `llama-cpp-python`)."""

from __future__ import annotations

import pytest

from neuropaca.sensing.stt_backend import FakeSttBackend, FasterWhisperBackend, SttBackend


def test_fake_stt_backend_returns_the_configured_transcript() -> None:
    backend = FakeSttBackend(transcript="open Brave")
    backend.load()
    assert backend.is_loaded is True
    assert backend.transcribe(b"\x00\x01", 16000) == "open Brave"


def test_fake_stt_backend_records_calls() -> None:
    backend = FakeSttBackend(transcript="hello")
    backend.transcribe(b"\x00\x00", 16000)
    assert backend.calls == [(b"\x00\x00", 16000)]


def test_fake_stt_backend_satisfies_the_protocol() -> None:
    assert isinstance(FakeSttBackend(), SttBackend)


def _skip_if_faster_whisper_installed() -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("faster-whisper is installed — this test covers the absent case")


def test_faster_whisper_backend_degrades_cleanly_without_the_package() -> None:
    _skip_if_faster_whisper_installed()

    # CI has no `faster-whisper` installed (pyproject.toml's `stt` extra is
    # optional) — `load()` must never raise, and `transcribe()` afterward
    # must return "" rather than touching a None model.
    backend = FasterWhisperBackend("medium")
    backend.load()
    assert backend.is_loaded is False
    assert backend.unavailable_reason is not None
    assert "faster-whisper" in backend.unavailable_reason
    assert backend.transcribe(b"\x00\x00", 16000) == ""


def test_faster_whisper_backend_load_is_idempotent() -> None:
    # Skipped when the real package is present, same as the test above: a
    # real load() downloads a multi-GB model over the network on first call
    # (rules.md §8 — "no test loads a real model"). `FakeSttBackend`'s own
    # idempotency is covered separately; this only checks the *self-disabled*
    # path calls load() twice safely.
    _skip_if_faster_whisper_installed()
    backend = FasterWhisperBackend("medium")
    backend.load()
    backend.load()
    assert backend.is_loaded is False


# gen-ref: 6949639d
