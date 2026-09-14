# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L2 · `VadGate` — the voice-activity pre-filter (A6.3, VISION_PHASES.md).

Silero VAD trims a push-to-talk buffer down to its speech-only spans before
anything reaches STT. Two things this buys, per the A6.3 spike: it rejects an
accidental button press with no real speech in it, and it avoids a well-known
Whisper failure mode — hallucinated text on silence/near-silence. Same
lazy-load / self-disable / `Fake*` discipline as `stt_backend.py` and
`core/inference.py`'s `LlamaCppBackend`.

**Fails open, not closed.** VAD is a latency/accuracy optimization here, not
a safety gate (unlike, say, the action tier system) — if the model can't
load, `trim()` passes the buffer through unfiltered rather than blocking
capture entirely (rules.md §2: a missing optional dependency degrades a
feature, it does not break one that doesn't need it).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

_log = logging.getLogger(__name__)


@runtime_checkable
class VadGate(Protocol):
    """What `voice_capture.py` drives."""

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def trim(self, pcm: bytes, sample_rate: int) -> bytes | None: ...


class SileroVadGate:
    """The real engine: Silero VAD, ONNX runtime (no torch dependency) —
    VISION_PHASES.md A6.3 spike #2. `load()` is the ONLY method that touches
    `silero_vad`; a missing package or a model that fails to open is logged
    and leaves `is_loaded` False, matching `FasterWhisperBackend.load()`.
    """

    def __init__(self, *, threshold: float = 0.5) -> None:
        self._threshold = threshold
        self._model: Any = None
        self.unavailable_reason: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            from silero_vad import load_silero_vad
        except ImportError as exc:
            self.unavailable_reason = f"silero-vad not installed ({exc})"
            _log.error("A6.3 VAD disabled — %s", self.unavailable_reason)
            return
        try:
            self._model = load_silero_vad(onnx=True)
        except Exception as exc:  # model download/open failure — never fatal (rules.md §2)
            self.unavailable_reason = f"failed to load Silero VAD model: {exc}"
            _log.error("A6.3 VAD disabled — %s", self.unavailable_reason)
            self._model = None

    def unload(self) -> None:
        self._model = None

    def trim(self, pcm: bytes, sample_rate: int) -> bytes | None:
        """`pcm` is 16-bit signed mono PCM at `sample_rate` Hz (Silero VAD
        only supports 8000/16000). Returns the speech-only concatenation of
        `pcm`, or `None` if no speech span was found at all — the caller
        should drop the buffer rather than hand silence to STT. Passes the
        buffer through unfiltered, unfiltered meaning untrimmed (not
        rejected), when the model isn't loaded (see module docstring)."""
        if self._model is None:
            return pcm

        import numpy as np
        from silero_vad import get_speech_timestamps

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        timestamps = get_speech_timestamps(
            audio, self._model, sampling_rate=sample_rate, threshold=self._threshold
        )
        if not timestamps:
            return None
        chunks = [audio[t["start"] : t["end"]] for t in timestamps]
        trimmed = np.concatenate(chunks)
        pcm16 = (trimmed * 32768.0).clip(-32768, 32767).astype(np.int16)
        return pcm16.tobytes()


class FakeVadGate:
    """Deterministic stand-in for tests. `should_reject` controls whether
    `trim()` reports silence (`None`) or speech present (the input,
    unchanged) — a fake buffer has no real waveform to detect activity in."""

    def __init__(self, *, should_reject: bool = False) -> None:
        self._loaded = False
        self.should_reject = should_reject
        self.calls: list[tuple[bytes, int]] = []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def trim(self, pcm: bytes, sample_rate: int) -> bytes | None:
        self.calls.append((pcm, sample_rate))
        return None if self.should_reject else pcm


# gen-ref: 2e0b5da8
