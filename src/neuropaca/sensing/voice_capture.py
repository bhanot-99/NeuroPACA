# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L2 · `VoiceCaptureModule` — push-to-talk capture -> VAD -> STT (A6.3,
VISION_PHASES.md).

Subscribes to `VOICE_PTT_STARTED`/`VOICE_PTT_STOPPED` — published by
`interface/activation.py`, never called directly (rules.md §0: "no module
imports another module. If you want a direct call, you want a new event.").
On stop: read back whatever the mic captured, optionally trim silence
(`sensing/vad.py`), transcribe (`sensing/stt_backend.py`), and append the
result to `config.voice_utterances_path` via `plugins/voice/voice_plugin.py`'s
`append_utterance()` — the exact file `VoicePlugin` already polls. **A6.1's
downstream pipeline (the plugin, the episode store, `VoiceIntentParser`,
`VoiceCommandParser`) needs zero changes for this** — that is the entire
point of A6.3's design (VISION_PHASES.md: "the plugin already accepts intent
text — this just changes where the text comes from").

One capture session at a time: a stray `_STARTED` while one is already
running, or a stray `_STOPPED` with none active, is logged and ignored, never
raised (rules.md §2 — a bus handler never raises).

`config.voice_ptt_max_seconds` bounds every session: a lost or never-sent
`_STOPPED` (a dropped tray click, a desynced toggle after a daemon or tray
restart) would otherwise leave the microphone capturing forever — an
unbounded buffer and an open mic with no hard stop. The timeout still
transcribes whatever was captured up to that point, same as a normal stop;
it only forces the *cutoff*, not a discard.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.stt_backend import SttBackend
from neuropaca.sensing.vad import VadGate

_log = logging.getLogger(__name__)

# Whisper's and Silero VAD's native rate — captured at this rate directly, no
# resampling step (VISION_PHASES.md A6.3 design).
SAMPLE_RATE_HZ = 16000


@runtime_checkable
class AudioSource(Protocol):
    """What `VoiceCaptureModule` drives to get raw microphone bytes. 16-bit
    signed mono PCM at `SAMPLE_RATE_HZ`, start-to-stop, whatever length."""

    def start(self) -> None: ...

    def stop(self) -> bytes: ...


class SoundDeviceSource:
    """The real microphone: `sounddevice.RawInputStream`, buffered by its own
    callback. Import is lazy, inside `start()` — the pattern every other
    optional backend in this codebase follows (`FasterWhisperBackend`,
    `SileroVadGate`, `LlamaCppBackend`) — so a machine without PortAudio
    installed gets an empty capture, not a crash."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE_HZ) -> None:
        self._sample_rate = sample_rate
        self._stream: Any = None
        self._chunks: list[bytes] = []
        self.unavailable_reason: str | None = None

    def start(self) -> None:
        self._chunks = []
        try:
            import sounddevice as sd
        except ImportError as exc:
            self.unavailable_reason = f"sounddevice not installed ({exc})"
            _log.error("A6.3 capture disabled — %s", self.unavailable_reason)
            return

        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                _log.warning("A6.3 capture stream status: %s", status)
            self._chunks.append(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._sample_rate, channels=1, dtype="int16", callback=_callback
            )
            self._stream.start()
        except Exception as exc:  # device busy/missing — never fatal (rules.md §2)
            self.unavailable_reason = f"failed to open microphone: {exc}"
            _log.error("A6.3 capture disabled — %s", self.unavailable_reason)
            self._stream = None

    def stop(self) -> bytes:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return b"".join(self._chunks)


class FakeAudioSource:
    """Deterministic stand-in for tests: `start()` is a no-op, `stop()`
    always returns whatever `pcm` this was constructed with."""

    def __init__(self, pcm: bytes = b"") -> None:
        self.pcm = pcm
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> bytes:
        self.stopped = True
        return self.pcm


class VoiceCaptureModule(BaseModule):
    """Ties capture -> VAD -> STT -> `append_utterance()` together."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        audio_source: AudioSource,
        vad_gate: VadGate,
        stt_backend: SttBackend,
        *,
        name: str = "voice_capture",
    ) -> None:
        super().__init__(name, event_bus, config)
        self._audio = audio_source
        self._vad = vad_gate
        self._stt = stt_backend
        self._session_active = False
        self._timeout_task: asyncio.Task[None] | None = None
        self._sessions = 0
        self._timed_out = 0
        self._rejected_silence = 0
        self._transcribed = 0
        self._empty_transcripts = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.VOICE_PTT_STARTED, self.on_ptt_started)
        self.event_bus.subscribe(EventType.VOICE_PTT_STOPPED, self.on_ptt_stopped)
        # Model loads are blocking (disk/network on first run) — offloaded so
        # daemon startup never stalls on them (rules.md §1), same as
        # `BitNetRuntime.load_model_async`.
        if not self._stt.is_loaded:
            await asyncio.to_thread(self._stt.load)
        if self.config.voice_vad_enabled and not self._vad.is_loaded:
            await asyncio.to_thread(self._vad.load)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.VOICE_PTT_STARTED, self.on_ptt_started)
        self.event_bus.unsubscribe(EventType.VOICE_PTT_STOPPED, self.on_ptt_stopped)
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timeout_task
            self._timeout_task = None

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._sessions} sessions ({self._timed_out} timed out) · "
                f"{self._transcribed} transcribed · "
                f"{self._rejected_silence} rejected (silence) · "
                f"{self._empty_transcripts} empty transcripts · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_ptt_started(self, event: Event) -> None:
        try:
            if self._session_active:
                _log.warning("A6.3: VOICE_PTT_STARTED while a session was already active")
                return
            self._session_active = True
            self._write_listening_state(True)
            self._sessions += 1
            await asyncio.to_thread(self._audio.start)
            self._timeout_task = asyncio.create_task(self._enforce_max_duration())
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._session_active = False
            self._write_listening_state(False)
            self._fail("on_ptt_started", exc)

    async def on_ptt_stopped(self, event: Event) -> None:
        try:
            if not self._session_active:
                _log.warning("A6.3: VOICE_PTT_STOPPED with no active session")
                return
            await self._finish_session()
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._fail("on_ptt_stopped", exc)

    async def _enforce_max_duration(self) -> None:
        """The hard cap `config.voice_ptt_max_seconds` documents (module
        docstring) — a lost `_STOPPED` must not leave the mic open forever."""
        try:
            await asyncio.sleep(self.config.voice_ptt_max_seconds)
        except asyncio.CancelledError:
            raise
        if not self._session_active:
            return  # finished normally already; this task just never got cancelled in time
        self._timed_out += 1
        _log.warning(
            "A6.3: session exceeded voice_ptt_max_seconds (%.1fs) — forcing stop",
            self.config.voice_ptt_max_seconds,
        )
        try:
            await self._finish_session()
        except Exception as exc:  # a background task never dies silently (rules.md §2)
            self._fail("_enforce_max_duration", exc)

    async def _finish_session(self) -> None:
        """The one path that ends a session, whether triggered by a real
        `_STOPPED` event or by `_enforce_max_duration`'s timeout — both need
        the exact same capture -> VAD -> STT -> file pipeline afterward.

        Writes `listening: false` immediately (before any of the slow VAD/
        STT work below) — the indicator should disappear the instant capture
        stops, not once transcription finishes.

        Always publishes `VOICE_PTT_SESSION_ENDED`, on every exit path
        (`finally`) — found by a live daemon run: `activation.py`'s tray
        toggle has no other way to learn a session ended via the timeout
        rather than a matching click, and without this it desyncs (the next
        click sends a STOP instead of the START the human expects)."""
        self._session_active = False
        self._write_listening_state(False)
        # Guard against cancelling ourselves: when `_enforce_max_duration`
        # calls this, it *is* `self._timeout_task` — cancelling it here would
        # raise CancelledError into the very next `await` below, aborting the
        # timeout path before it could stop the mic or transcribe anything.
        if self._timeout_task is not None and self._timeout_task is not asyncio.current_task():
            self._timeout_task.cancel()
        self._timeout_task = None

        try:
            pcm = await asyncio.to_thread(self._audio.stop)
            self._last_at = datetime.now(UTC)
            if not pcm:
                return

            if self.config.voice_vad_enabled:
                maybe_trimmed = await asyncio.to_thread(self._vad.trim, pcm, SAMPLE_RATE_HZ)
                if maybe_trimmed is None:
                    self._rejected_silence += 1
                    return
                trimmed = maybe_trimmed
            else:
                trimmed = pcm

            text = await asyncio.to_thread(self._stt.transcribe, trimmed, SAMPLE_RATE_HZ)
            if not text.strip():
                self._empty_transcripts += 1
                return

            from plugins.voice.voice_plugin import append_utterance

            await asyncio.to_thread(
                append_utterance, self.config.voice_utterances_path, text.strip()
            )
            self._transcribed += 1
        finally:
            self.event_bus.publish(
                Event(event_type=EventType.VOICE_PTT_SESSION_ENDED, source=self.name, payload={})
            )

    def _write_listening_state(self, listening: bool) -> None:
        """The daemon's one write-back for the tray's "listening" indicator
        (`scripts/neuropaca_tray.py`'s `read_listening_state()`) — same
        atomic tmp+replace convention as `orchestrator.py`'s health dump and
        the tray's own `write_ptt_trigger()`. Called synchronously (no
        `asyncio.to_thread`) on purpose: this is a session start/end, a rare
        event, not a hot path, and `orchestrator._write_health_dump()`
        already sets the precedent that a write this small and this
        infrequent doesn't need offloading. Never raises (rules.md §2) — a
        stale indicator is a UI nit, not a reason to fail the session."""
        try:
            path = Path(self.config.voice_listening_state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"listening": listening})
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            _log.warning("A6.3: failed to write listening-state indicator: %s", exc)

    def _fail(self, where: str, exc: Exception) -> None:
        self._errors += 1
        _log.exception("voice_capture %s failed", where)
        self.event_bus.publish(
            system_error_event(module=self.name, exception=str(exc), severity="handler")
        )


# gen-ref: 858f38e0
