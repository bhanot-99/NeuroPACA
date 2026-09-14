# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L9 · `VoiceActivationModule` — push-to-talk activation (A6.3, VISION_PHASES.md).

Publishes `VOICE_PTT_STARTED`/`VOICE_PTT_STOPPED`; never calls
`sensing/voice_capture.py` directly (rules.md §0). Two trigger sources,
selected by `config.voice_activation_mode`, both meant to drive the identical
downstream path (VISION_PHASES.md A6.3's own test requirement):

- **`"tray"` (the shipped default).** `spikes/a6_3_portal/` found this
  machine's COSMIC session does not implement the
  `org.freedesktop.portal.GlobalShortcuts` portal, so a global hotkey isn't
  available here today. The tray (`scripts/neuropaca_tray.py`) runs as a
  separate process under system Python with no access to this daemon's
  in-process state — same reason it already reads `health_dump_path` as a
  file instead of a live call — so a tray click writes a small JSON trigger
  file (`config.voice_ptt_trigger_path`) instead, and this module polls it.
  A tray click is a discrete event, not a press-and-hold, so the tray models
  push-to-talk as a **toggle**: click once to start, click again to stop.

- **`"hotkey"`.** Deliberately a capability check only, not a working
  implementation. `GlobalShortcuts`' real API (`CreateSession` /
  `BindShortcuts` / an `Activated` signal carrying a session handle) is
  involved enough that writing it against a portal this machine cannot
  actually answer would be exactly the thing rules.md §9 forbids — "write
  placeholder code that pretends to work." `initialize()` re-runs the same
  read-only introspection the spike used; if the interface is missing (true
  on this machine today), the module logs that once and reports
  `hotkey: unavailable` from `health()` forever after, publishing nothing.
  Wiring the real session/signal handshake is future work, gated on a
  compositor that actually implements the portal to test it against.

- **`"wake_word"`.** User decision 2026-09-14, overriding A6's original
  "push-to-talk only, no ambient listening" call (memory:
  `voice-feature-plan-and-privacy-decisions.md` records the override). The
  mic runs continuously via `sensing/wake_word.py`'s `WakeWordAudioSource`,
  but nothing is recorded or transcribed pre-trigger — only a per-frame
  confidence score, immediately discarded. When `config.voice_wake_word_phrase`
  ("hey_jarvis", the shipped default — a pre-trained openWakeWord model, no
  custom training) scores above `voice_wake_word_threshold`: the continuous
  tap pauses (one mic consumer at a time, not two racing streams), a normal
  `VOICE_PTT_STARTED` fires (the *same* event `VoiceCaptureModule` already
  handles — no new capture path), and a `voice_wake_word_listen_seconds`
  timer (shorter than, and independent of, `VoiceCaptureModule`'s own
  `voice_ptt_max_seconds` safety cap) auto-publishes `VOICE_PTT_STOPPED` if
  nothing else does first. `VOICE_PTT_SESSION_ENDED` (see below) resumes the
  continuous tap once the command capture finishes. This is a fixed-window
  v1, not true trailing-silence end-of-utterance detection — an honest
  scope call, not an oversight (module's own inline notes explain why).

- **`"both"`.** User decision 2026-09-14: run the tray toggle and the
  wake-word tap at the same time, so either one can start a session. The two
  sources publish the exact same `VOICE_PTT_STARTED`/`_STOPPED` events, and
  `VoiceCaptureModule.on_ptt_started` already no-ops a second START while a
  session is active, so a stray click during a wake-word-triggered session
  (or vice versa) cannot double-start a capture. What it *can* do is fight
  over the microphone — one `sounddevice` stream for the continuous tap, a
  separate one `VoiceCaptureModule` opens for the actual capture — so
  whichever source starts a session first pauses the wake-word tap
  (`_pause_wake_word_tap`), and `on_session_ended` resumes it once the
  session is over, regardless of which source started it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.wake_word import WakeWordAudioSource, WakeWordDetector

_log = logging.getLogger(__name__)

# How often the tray-trigger file is polled. A local file stat/read at this
# cadence is not measurable CPU (the tray itself polls its own read side
# every 5s at the same order of magnitude) — set faster than that here
# because this is now on the user's perceived-latency path, a push-to-talk
# button that visibly lags feels broken in a way a status icon refreshing
# late does not.
_TRIGGER_POLL_SECONDS = 0.4

_PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
_PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_GLOBAL_SHORTCUTS_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"


def _read_trigger(path: Path) -> dict[str, Any] | None:
    """The trigger file, parsed — `None` on any failure (absent, unreadable,
    malformed), same "every failure mode looks like no data" discipline as
    the tray's own `read_health()`."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def probe_global_shortcuts_portal() -> bool:
    """Read-only: does this session's desktop portal implement
    `GlobalShortcuts`? Same check as `spikes/a6_3_portal/
    probe_global_shortcuts.sh`, callable from Python so `initialize()` can
    re-run it rather than trusting a value frozen at spike time. Shells out
    to `gdbus` (already how `interface/notifier.py` talks to the desktop,
    rather than a raw D-Bus binding) — missing `gdbus`, a timeout, or any
    non-zero exit is treated as "not available", never raised."""
    try:
        result = subprocess.run(
            [
                "gdbus",
                "introspect",
                "--session",
                "--dest",
                _PORTAL_BUS_NAME,
                "--object-path",
                _PORTAL_OBJECT_PATH,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("A6.3 portal probe failed: %s", exc)
        return False
    return result.returncode == 0 and _GLOBAL_SHORTCUTS_INTERFACE in result.stdout


class VoiceActivationModule(BaseModule):
    """Watches whichever trigger `config.voice_activation_mode` selects and
    publishes `VOICE_PTT_STARTED`/`VOICE_PTT_STOPPED`.

    Subscribes to `VOICE_PTT_SESSION_ENDED` (published by `voice_capture.py`
    on every session end, including its own internal `voice_ptt_max_seconds`
    timeout) purely to reset the tray toggle — found necessary by a live
    daemon run: without it, a session that auto-ends via the timeout leaves
    `_toggle_active` at `True` with nobody having told this module the
    capture already stopped, so the *next* click sends a STOP instead of the
    START the human just pressed for. Resetting on every session-end is safe
    even when the toggle already matches (a normal stop-before-timeout) —
    setting it to `False` twice is a no-op.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        *,
        name: str = "voice_activation",
        wake_word_detector: WakeWordDetector | None = None,
        wake_word_audio_source: WakeWordAudioSource | None = None,
    ) -> None:
        super().__init__(name, event_bus, config)
        self._task: asyncio.Task[None] | None = None
        self._last_seq: int | None = None
        self._toggle_active = False
        self._hotkey_available: bool | None = None  # None = not checked yet
        self._triggers = 0
        self._errors = 0
        self._last_at: datetime | None = None
        # Only meaningful when voice_activation_mode == "wake_word"; unused
        # (and may be None) for "tray"/"hotkey".
        self._wake_word = wake_word_detector
        self._wake_word_audio = wake_word_audio_source
        self._listening_for_command = False
        self._wake_triggers = 0
        self._wake_word_timeout_task: asyncio.Task[None] | None = None
        self._wake_word_tap_active = False

    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.VOICE_PTT_SESSION_ENDED, self.on_session_ended)
        mode = self.config.voice_activation_mode
        if mode in ("tray", "both"):
            path = Path(self.config.voice_ptt_trigger_path)
            trigger = await asyncio.to_thread(_read_trigger, path)
            if trigger is not None:
                self._last_seq = trigger.get("seq")
        if mode == "hotkey":
            self._hotkey_available = await asyncio.to_thread(probe_global_shortcuts_portal)
            if not self._hotkey_available:
                _log.warning(
                    "A6.3: voice_activation_mode='hotkey' but this session's portal does "
                    "not implement GlobalShortcuts — activation will never fire. "
                    "Switch voice_activation_mode to 'tray', or see "
                    "spikes/a6_3_portal/README.md."
                )
        if mode in ("wake_word", "both") and self._wake_word is not None:
            if not self._wake_word.is_loaded:
                await asyncio.to_thread(self._wake_word.load)

    async def start(self) -> None:
        self.is_running = True
        mode = self.config.voice_activation_mode
        if mode in ("tray", "both"):
            self._task = asyncio.create_task(self._poll_trigger_file())
        if mode in ("wake_word", "both"):
            self._start_wake_word_listening()

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.VOICE_PTT_SESSION_ENDED, self.on_session_ended)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._wake_word_timeout_task is not None:
            self._wake_word_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_word_timeout_task
            self._wake_word_timeout_task = None
        self._pause_wake_word_tap()

    async def on_session_ended(self, event: Event) -> None:
        self._toggle_active = False
        if self._listening_for_command:
            self._listening_for_command = False
            if self._wake_word_timeout_task is not None:
                self._wake_word_timeout_task.cancel()
                self._wake_word_timeout_task = None
        # Resume the always-on tap for EITHER trigger source that started the
        # session that just ended — a tray-triggered session pauses the tap
        # too (see _handle_trigger), so this must not be gated on
        # `_listening_for_command` alone or "both" mode would never resume
        # listening after a tray-started session. No-op if already listening.
        if self.is_running and self.config.voice_activation_mode in ("wake_word", "both"):
            self._start_wake_word_listening()

    def health(self) -> ModuleHealth:
        mode = self.config.voice_activation_mode
        parts: list[str] = []
        if mode == "hotkey":
            parts.append(f"hotkey: {'available' if self._hotkey_available else 'unavailable'}")
        if mode in ("wake_word", "both"):
            loaded = self._wake_word is not None and self._wake_word.is_loaded
            parts.append(
                f"wake_word: {'loaded' if loaded else 'unavailable'} "
                f"({self.config.voice_wake_word_phrase!r}) · {self._wake_triggers} triggers"
            )
        if mode in ("tray", "both"):
            parts.append(f"tray trigger file: {self.config.voice_ptt_trigger_path}")
        detail = " · ".join(parts) if parts else "no trigger source configured"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=f"mode={mode} · {detail} · {self._triggers} triggers · {self._errors} errors",
            last_event_at=self._last_at,
        )

    # -------------------------------------------------------- wake-word mode
    def _start_wake_word_listening(self) -> None:
        if self._wake_word is None or self._wake_word_audio is None:
            return
        if not self._wake_word.is_loaded:
            _log.warning("A6.3: wake-word model not loaded — listening will never fire")
            return
        if self._wake_word_tap_active:
            return  # already listening — starting again would open a second stream
        loop = asyncio.get_running_loop()

        def _on_frame(frame: bytes) -> None:
            # Runs on PortAudio's own callback thread, not the event loop —
            # score() must stay cheap (openWakeWord's whole design point),
            # and the trigger itself is marshalled back via
            # call_soon_threadsafe rather than touched here directly.
            score = self._wake_word.score(frame) if self._wake_word is not None else 0.0
            if score >= self.config.voice_wake_word_threshold:
                loop.call_soon_threadsafe(self._on_wake_word_detected)

        self._wake_word_audio.start(_on_frame)
        self._wake_word_tap_active = True

    def _pause_wake_word_tap(self) -> None:
        """Stop the always-on tap so it never competes with the mic stream
        `VoiceCaptureModule` opens for the actual capture (one consumer at a
        time). Called whenever ANY trigger source starts a session — not just
        a wake-word detection — so "both" mode's tray path doesn't fight the
        tap for the microphone. Idempotent: safe to call when already paused.
        """
        if self._wake_word_audio is not None and self._wake_word_tap_active:
            self._wake_word_audio.stop()
        self._wake_word_tap_active = False

    def _on_wake_word_detected(self) -> None:
        if self._listening_for_command:
            return  # already triggered; ignore re-detections mid-command
        self._listening_for_command = True
        self._wake_triggers += 1
        self._last_at = datetime.now(UTC)
        self._pause_wake_word_tap()
        self.event_bus.publish(
            Event(event_type=EventType.VOICE_PTT_STARTED, source=self.name, payload={})
        )
        self._wake_word_timeout_task = asyncio.create_task(self._auto_stop_after_wake_word())

    async def _auto_stop_after_wake_word(self) -> None:
        try:
            await asyncio.sleep(self.config.voice_wake_word_listen_seconds)
        except asyncio.CancelledError:
            raise
        if not self._listening_for_command:
            return
        self.event_bus.publish(
            Event(event_type=EventType.VOICE_PTT_STOPPED, source=self.name, payload={})
        )

    # ------------------------------------------------------------- tray mode
    async def _poll_trigger_file(self) -> None:
        path = Path(self.config.voice_ptt_trigger_path)
        while True:
            try:
                await asyncio.sleep(_TRIGGER_POLL_SECONDS)
                trigger = await asyncio.to_thread(_read_trigger, path)
                self._handle_trigger(trigger)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a background task never dies silently (rules.md §2)
                self._fail("_poll_trigger_file", exc)

    def _handle_trigger(self, trigger: dict[str, Any] | None) -> None:
        if trigger is None:
            return
        seq = trigger.get("seq")
        if not isinstance(seq, int) or seq == self._last_seq:
            return
        self._last_seq = seq
        self._triggers += 1
        self._last_at = datetime.now(UTC)

        # A tray click is a toggle (module docstring): the first click after
        # start-up or after a completed session starts a capture; the next
        # one stops it. The trigger file carries only `seq`/`ts` — no state
        # of its own to trust or distrust — because the tray and this module
        # could disagree about whose turn it is after either one restarts;
        # local toggle state, keyed off "did seq change", sidesteps that.
        self._toggle_active = not self._toggle_active
        if self._toggle_active:
            self._pause_wake_word_tap()  # "both" mode: don't fight over the mic
        event_type = (
            EventType.VOICE_PTT_STARTED if self._toggle_active else EventType.VOICE_PTT_STOPPED
        )
        self.event_bus.publish(Event(event_type=event_type, source=self.name, payload={}))

    def _fail(self, where: str, exc: Exception) -> None:
        self._errors += 1
        _log.exception("voice_activation %s failed", where)
        self.event_bus.publish(
            system_error_event(module=self.name, exception=str(exc), severity="handler")
        )


# gen-ref: 6e243c89
