# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L2 · `SttBackend` — the speech-to-text seam (A6.3, VISION_PHASES.md).

Mirrors `core/inference.py`'s `InferenceBackend`/`FakeInferenceBackend` split
exactly: one `Protocol` every caller depends on, a lazy-loaded real
implementation that self-disables on a missing dependency or model, and a
deterministic fake for tests. Nothing above this module ever imports
`faster_whisper` directly (rules.md §4: "Backends implement the ... Protocol.
Module code never imports a backend directly.").

`transcribe()` is BLOCKING, same discipline as `InferenceBackend.infer()` —
the caller (`voice_capture.py`) offloads it via `asyncio.to_thread`; this
class does no async work itself.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

_log = logging.getLogger(__name__)


@runtime_checkable
class SttBackend(Protocol):
    """What `VoiceCaptureModule` drives."""

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def transcribe(self, pcm: bytes, sample_rate: int) -> str: ...


class FasterWhisperBackend:
    """The real engine: `faster-whisper` (CTranslate2, INT8) — VISION_PHASES.md
    A6.3 spike #1. `load()` is the ONLY method that touches `faster_whisper`,
    and it is defensive: a missing package, or a model that fails to open, is
    logged, leaves `is_loaded` False, and every later `transcribe()` returns
    `""` instead of raising (rules.md §2) — `LlamaCppBackend`'s exact shape.
    """

    def __init__(self, model_size: str, *, language: str = "en", n_threads: int = 4) -> None:
        self._model_size = model_size
        self._language = language
        self._n_threads = n_threads
        self._model: Any = None
        self.unavailable_reason: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            self.unavailable_reason = f"faster-whisper not installed ({exc})"
            _log.error("A6.3 STT disabled — %s", self.unavailable_reason)
            return
        try:
            self._model = WhisperModel(
                self._model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=self._n_threads,
            )
        except Exception as exc:  # model download/open failure — never fatal (rules.md §2)
            self.unavailable_reason = (
                f"failed to load faster-whisper model {self._model_size!r}: {exc}"
            )
            _log.error("A6.3 STT disabled — %s", self.unavailable_reason)
            self._model = None

    def unload(self) -> None:
        self._model = None

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """`pcm` is 16-bit signed mono PCM at `sample_rate` Hz — the shape
        `voice_capture.py`'s `AudioSource` produces. `numpy` is imported here,
        not at module level: it only ever runs once `faster_whisper` itself
        already imported successfully, so it rides along with the optional
        `stt` extra rather than becoming a new always-on dependency."""
        if self._model is None:
            return ""
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(audio, language=self._language, vad_filter=False)
        return " ".join(seg.text.strip() for seg in segments).strip()


class FakeSttBackend:
    """Deterministic stand-in for tests and any config with no real STT
    engine configured. A fake microphone's bytes carry no meaningful signal
    to vary output on (unlike `FakeInferenceBackend`'s prompt/grammar), so
    tests set the expected transcript explicitly via `next_transcript`
    instead of deriving it from the input."""

    def __init__(self, transcript: str = "") -> None:
        self._loaded = False
        self.next_transcript = transcript
        self.calls: list[tuple[bytes, int]] = []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls.append((pcm, sample_rate))
        return self.next_transcript


# gen-ref: aac1f94e
