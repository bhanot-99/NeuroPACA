# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L2 · `WakeWordDetector`/`WakeWordAudioSource` — always-on wake-phrase
detection (A6.3, VISION_PHASES.md).

User decision (2026-09-14) overriding A6's original "push-to-talk only, no
ambient listening" call — see memory's
`voice-feature-plan-and-privacy-decisions.md` for the override itself. The
privacy property that decision was protecting is preserved differently, not
dropped: the microphone is open continuously, but only a rolling per-80ms-
frame confidence score is ever computed. **No audio frame is written to
disk, logged, or accumulated anywhere in this module** — `score()` and the
audio callback both discard the frame the moment they're done with it.
Recording only starts (via the *existing*, unmodified
`VOICE_PTT_STARTED`/`_STOPPED` path — `interface/activation.py` owns that
wiring) once the phrase actually fires.

Same lazy-load / self-disable / `Fake*` discipline as `stt_backend.py` and
`vad.py`. `hey jarvis` (the shipped default, `config.voice_wake_word_phrase`)
is a pre-trained openWakeWord model — no custom training, per the same
decision that chose a pre-built phrase over training "buddy" from scratch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

# openWakeWord's own recommended frame size: 80ms at 16kHz, 16-bit mono.
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * 2


@runtime_checkable
class WakeWordDetector(Protocol):
    """What `VoiceActivationModule` drives, one frame at a time."""

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def score(self, frame: bytes) -> float: ...


class OpenWakeWordDetector:
    """The real engine: openWakeWord (Apache-2.0, ONNX/tflite). `load()` is
    the ONLY method that touches `openwakeword`; a missing package or a
    phrase that fails to download/open is logged, leaves `is_loaded` False,
    and `score()` returns `0.0` instead of raising (rules.md §2) —
    `FasterWhisperBackend`'s exact shape."""

    def __init__(self, phrase: str) -> None:
        self._phrase = phrase
        self._model: Any = None
        self.unavailable_reason: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import openwakeword
            from openwakeword.model import Model
        except ImportError as exc:
            self.unavailable_reason = f"openwakeword not installed ({exc})"
            _log.error("A6.3 wake-word disabled — %s", self.unavailable_reason)
            return
        try:
            openwakeword.utils.download_models(model_names=[self._phrase])
            self._model = Model(wakeword_models=[self._phrase])
        except Exception as exc:  # model download/open failure — never fatal (rules.md §2)
            self.unavailable_reason = f"failed to load wake-word phrase {self._phrase!r}: {exc}"
            _log.error("A6.3 wake-word disabled — %s", self.unavailable_reason)
            self._model = None

    def unload(self) -> None:
        self._model = None

    def score(self, frame: bytes) -> float:
        """`frame` is 16-bit signed mono PCM, `FRAME_BYTES` long, at 16kHz.
        Returns the confidence score in `[0, 1]` for `self._phrase` in this
        one frame — nothing about `frame` is retained after this call
        returns (module docstring: nothing is accumulated pre-trigger)."""
        if self._model is None:
            return 0.0
        import numpy as np

        audio = np.frombuffer(frame, dtype=np.int16)
        predictions: dict[str, float] = self._model.predict(audio)
        return float(predictions.get(self._phrase, 0.0))


class FakeWakeWordDetector:
    """Deterministic stand-in for tests. `next_score` controls what
    `score()` returns regardless of the frame's actual content — a fake
    frame carries no real waveform to detect a phrase in."""

    def __init__(self, next_score: float = 0.0) -> None:
        self._loaded = False
        self.next_score = next_score
        self.calls: list[bytes] = []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def score(self, frame: bytes) -> float:
        self.calls.append(frame)
        return self.next_score


@runtime_checkable
class WakeWordAudioSource(Protocol):
    """A continuous microphone tap — unlike `voice_capture.py`'s
    `AudioSource` (accumulate everything, hand it all back on `stop()`),
    this calls `on_frame` once per `FRAME_BYTES` chunk and holds nothing
    itself. The caller (`VoiceActivationModule`) decides what happens to
    each frame; this class never persists one."""

    def start(self, on_frame: Callable[[bytes], None]) -> None: ...

    def stop(self) -> None: ...


class SoundDeviceWakeWordSource:
    """The real always-on tap: `sounddevice.RawInputStream`, fixed at
    `FRAME_SAMPLES` per callback. Import is lazy, inside `start()` — same
    pattern as `voice_capture.py`'s `SoundDeviceSource` — so a machine
    without PortAudio installed just never calls `on_frame`, never crashes.
    """

    def __init__(self, *, sample_rate: int = 16000) -> None:
        self._sample_rate = sample_rate
        self._stream: Any = None
        self.unavailable_reason: str | None = None

    def start(self, on_frame: Callable[[bytes], None]) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            self.unavailable_reason = f"sounddevice not installed ({exc})"
            _log.error("A6.3 wake-word listener disabled — %s", self.unavailable_reason)
            return

        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                _log.warning("A6.3 wake-word stream status: %s", status)
            on_frame(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=_callback,
            )
            self._stream.start()
        except Exception as exc:  # device busy/missing — never fatal (rules.md §2)
            self.unavailable_reason = f"failed to open microphone: {exc}"
            _log.error("A6.3 wake-word listener disabled — %s", self.unavailable_reason)
            self._stream = None

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class FakeWakeWordAudioSource:
    """Deterministic stand-in for tests: `start()` stores the callback so a
    test can push frames manually via `push_frame()`, without any real
    audio device."""

    def __init__(self) -> None:
        self._on_frame: Callable[[bytes], None] | None = None
        self.started = False
        self.stopped = False
        self.start_count = 0
        self.stop_count = 0

    def start(self, on_frame: Callable[[bytes], None]) -> None:
        self.start_count += 1
        self.started = True
        self._on_frame = on_frame

    def stop(self) -> None:
        self.stop_count += 1
        self.stopped = True
        self._on_frame = None

    def push_frame(self, frame: bytes) -> None:
        if self._on_frame is not None:
            self._on_frame(frame)


# gen-ref: cecfcd3c
