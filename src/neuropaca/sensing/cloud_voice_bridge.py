# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L2 · `GeminiBridgeSttBackend` — the opt-in cloud speed path for STT
(user decision 2026-09-14, VISION_PHASES.md A6.3).

Implements the exact same `SttBackend` protocol (`sensing/stt_backend.py`)
as `FasterWhisperBackend` — `VoiceCaptureModule` never knows or cares which
one it's holding (rules.md §4). What's different is *where the transcription
actually runs*: never inside this process. `neuropacad` keeps
`PrivateNetwork=true` — that hardening is unchanged and this class never
imports anything network-capable. Instead it drops the captured audio as a
WAV file into `voice_cloud_bridge_dir/pending/` and polls
`voice_cloud_bridge_dir/responses/` for a matching reply, written by
`scripts/voice_cloud_helper.py` — a completely separate process, its own
systemd unit, with its own scoped network access, which is the only thing
in this whole feature that ever talks to Gemini. Same file-drop handshake
`interface/activation.py`'s tray trigger already uses, just with a request
and a response file instead of one.

Fails toward the local model, not toward silence: any timeout, missing
helper, malformed response, or an explicit `{"error": ...}` from the helper
all fall through to `transcribe()`'s wrapped `local_fallback` — so voice
keeps working (just slower) through a helper crash, an expired/missing API
key, or a dead wifi connection, rather than going silent (rules.md §2).

`transcribe()` is BLOCKING, same discipline as `FasterWhisperBackend`'s —
`VoiceCaptureModule` already offloads it via `asyncio.to_thread`, so the
polling loop's `time.sleep` here costs a thread, not the event loop.
"""

from __future__ import annotations

import json
import logging
import time
import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from neuropaca.sensing.stt_backend import SttBackend

_log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = (
    0.15  # matches interface/activation.py's trigger-file cadence order of magnitude
)


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """16-bit signed mono PCM (the shape every `AudioSource` produces,
    `sensing/voice_capture.py`) wrapped in a minimal WAV container — what
    `scripts/voice_cloud_helper.py` sends to Gemini as `audio/wav`."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buf.getvalue()


class GeminiBridgeSttBackend:
    """`SttBackend` that hands audio to a separate, network-capable helper
    process instead of transcribing locally. See module docstring."""

    def __init__(
        self,
        local_fallback: SttBackend,
        *,
        bridge_dir: str,
        timeout_seconds: float,
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._local = local_fallback
        self._request_dir = Path(bridge_dir) / "pending"
        self._response_dir = Path(bridge_dir) / "responses"
        self._timeout_seconds = timeout_seconds
        self._id_factory = id_factory
        self._cloud_hits = 0
        self._fallback_hits = 0
        self._errors = 0
        self.last_fallback_reason: str | None = None

    @property
    def is_loaded(self) -> bool:
        # There is no cloud "model" resident in this process to be loaded —
        # readiness is the local fallback's, since that's what actually runs
        # whenever the helper doesn't answer in time.
        return self._local.is_loaded

    def load(self) -> None:
        self._request_dir.mkdir(parents=True, exist_ok=True)
        self._response_dir.mkdir(parents=True, exist_ok=True)
        self._local.load()

    def unload(self) -> None:
        self._local.unload()

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        request_id = self._id_factory()
        request_path = self._request_dir / f"{request_id}.wav"
        response_path = self._response_dir / f"{request_id}.json"
        try:
            self._write_request(request_path, pcm, sample_rate)
            transcript = self._await_response(response_path)
            if transcript is not None:
                self._cloud_hits += 1
                return transcript
        except Exception as exc:  # never let a bridge bug take voice down (rules.md §2)
            self._errors += 1
            self.last_fallback_reason = f"bridge error: {exc}"
            _log.exception("cloud_voice_bridge transcribe() failed, falling back to local")
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

        self._fallback_hits += 1
        return self._local.transcribe(pcm, sample_rate)

    def _write_request(self, request_path: Path, pcm: bytes, sample_rate: int) -> None:
        # Atomic write (tmp + rename), same convention as
        # voice_capture.py's _write_listening_state: the helper's polling
        # loop must never see a partially-written WAV file.
        tmp_path = request_path.with_suffix(".wav.tmp")
        tmp_path.write_bytes(_pcm_to_wav_bytes(pcm, sample_rate))
        tmp_path.rename(request_path)

    def _await_response(self, response_path: Path) -> str | None:
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    payload = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    self.last_fallback_reason = f"malformed response: {exc}"
                    return None
                error = payload.get("error")
                if error:
                    self.last_fallback_reason = f"helper reported: {error}"
                    return None
                transcript = payload.get("transcript")
                return transcript if isinstance(transcript, str) else None
            time.sleep(_POLL_INTERVAL_SECONDS)
        self.last_fallback_reason = f"no response within {self._timeout_seconds}s"
        return None
