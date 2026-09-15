"""
Always-on voice daemon. Two independent ways to trigger a command, exactly
as ARCHITECTURE.md's Step 5 requires ("push-to-talk stays as the permanent
manual fallback — it doesn't get removed"):

  1. Manual: the tray (tray.py) toggles over a FIFO — click 1 starts, click
     2 stops. No wake word needed, no VAD — the click IS the stop signal,
     exactly as precise as before.
  2. Wake word: say "hey jarvis" (openwakeword's bundled hey_jarvis model,
     the same wake word the old, since-removed A6.3 implementation used).
     Recording then stops automatically via VAD-based silence detection
     (openwakeword's bundled Silero VAD wrapper) — this is "turn detection"
     in the plain sense the doc means: reads whether you're still talking,
     not a flat timer.

One continuous audio stream feeds a small state machine (idle / recording),
rather than opening a fresh sd.InputStream per command as main.py's simpler
push-to-talk does — wake-word detection has to listen continuously.

Asleep (session_state, the sleep_stop_listening skill) disables WAKE-WORD
detection specifically — that skill is named "stop listening" for a reason.
The tray's manual toggle keeps working regardless; it's the only way to
say "wake up" while wake-word listening is paused.
"""

import contextlib
import io
import os
import queue
import tempfile
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf
from openwakeword.model import Model
from openwakeword.vad import VAD

import actions
import config
import llm_intent
import skills
import stt
from config import SAMPLE_RATE
from confirm_loop import confirm_and_run
from skills import _audit, _session_state, _tiers, semantic_match

FIFO_PATH = os.path.expanduser("~/.local/share/voice-standalone/toggle.fifo")

CHUNK_SIZE = 1280  # 80ms @ 16kHz — openwakeword's expected input size; the
                    # bundled VAD accepts the same size directly (verified
                    # live: vad.predict(chunk, frame_size=1280) works fine,
                    # no separate re-chunking needed for the two models).
WAKE_THRESHOLD = 0.5
SPEECH_VAD_THRESHOLD = 0.5
SILENCE_STOP_SECONDS = 1.2      # continuous non-speech that ends a wake-word turn
NO_SPEECH_TIMEOUT_SECONDS = 4.0  # abandon if nothing is said after the wake word
MAX_RECORDING_SECONDS = 20.0     # safety cap regardless of VAD

_HEY_JARVIS_PATH = os.path.join(
    os.path.dirname(__import__("openwakeword").__file__),
    "resources", "models", "hey_jarvis_v0.1.onnx",
)

_manual_toggle_event = threading.Event()


def _notify(title: str, body: str) -> None:
    import subprocess
    body = body.strip()
    if len(body) > 300:
        body = body[:300] + "…"
    subprocess.run(["notify-send", "--app-name=Voice Assistant", title, body], check=False)


def _ensure_fifo() -> None:
    os.makedirs(os.path.dirname(FIFO_PATH), exist_ok=True)
    if not os.path.exists(FIFO_PATH):
        os.mkfifo(FIFO_PATH)


def _fifo_listener() -> None:
    """Runs forever in a background thread — each message sets the shared
    event; the capture loop below clears it after acting on it."""
    while True:
        try:
            with open(FIFO_PATH) as f:
                f.readline()
            _manual_toggle_event.set()
        except OSError:
            time.sleep(1)


def _save_wav(frames: list[np.ndarray], path: str) -> None:
    audio = np.concatenate(frames, axis=0) if frames else np.zeros((0,), dtype="int16")
    sf.write(path, audio, SAMPLE_RATE)


def _process_command(wav_path: str, layer1_available: bool) -> None:
    """Shared by both trigger paths once a recording is ready: transcribe,
    resolve, execute, notify. Identical to main.py's own dispatch logic."""
    try:
        text = stt.transcribe(wav_path)
    except Exception as exc:
        _notify("Voice Assistant — error", f"Could not transcribe that: {exc}")
        return
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    if not text:
        _notify("Voice Assistant", "(heard nothing)")
        return

    try:
        name, args = skills.match_skill(text)
        if name is None and layer1_available:
            name, args = semantic_match.match(text)
        if name is None:
            name, args = llm_intent.resolve_intent(text)

        if name is None:
            _notify(f"Heard: {text}", "No matching action.")
            return

        if _session_state.is_asleep() and name != "wake_back_up":
            _notify("Voice Assistant", "(asleep — say 'wake up' to resume)")
            return

        tier = _tiers.tier_of(name)

        if tier == "DANGEROUS" and config.SAFETY_TIERS_ENABLED:
            gen = confirm_and_run(name, args)
            pause = next(gen)
            warning_text = ("\n⚠ " + "; ".join(pause["warnings"])) if pause["warnings"] else ""
            _notify(
                f"Confirm: {pause['preview']}",
                f"Click the tray within 10s to run it.{warning_text}\n"
                "(No text editing from the tray yet — main.py's terminal mode "
                "supports editing the command before confirming.)",
            )
            confirmed = _manual_toggle_event.wait(timeout=10)
            if confirmed:
                _manual_toggle_event.clear()
            try:
                gen.send(None if confirmed else "")
                result = None
            except StopIteration as done:
                result = done.value
            if result and result["cancelled"]:
                _notify("Voice Assistant", "Cancelled (no confirmation within 10s).")
                _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="cancelled")
                return
            output = (result["output"].strip() if result else "") or f"Ran: {name}"
            _session_state.set_last_response(output)
            _notify(f"Heard: {text}", output)
            _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
            return

        if tier == "REVIEW" and config.SAFETY_TIERS_ENABLED:
            _notify(f"About to run: {name}", str(args))
            time.sleep(2)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            actions.DISPATCH[name](args)
        output = buffer.getvalue().strip() or f"Ran: {name}"
        _session_state.set_last_response(output)
        _notify(f"Heard: {text}", output)
        _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
    except Exception as exc:
        _notify("Voice Assistant — error", f"Could not complete that command: {exc}")


def main() -> None:
    _ensure_fifo()

    print("Loading local semantic matcher...")
    layer1_available = True
    try:
        semantic_match._ensure_index_built()
    except Exception as exc:
        layer1_available = False
        print(f"[warning] Layer 1 unavailable this session: {exc}")

    print("Loading wake-word model (hey jarvis) and VAD...")
    wake_model = Model(wakeword_model_paths=[_HEY_JARVIS_PATH], vad_threshold=0.0)
    vad = VAD()

    threading.Thread(target=_fifo_listener, daemon=True).start()

    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def _callback(indata, _frames, _time_info, _status) -> None:
        audio_q.put(indata.copy().flatten())

    _notify("Voice Assistant", "Ready — click the tray icon, or say 'hey jarvis.'")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                         blocksize=CHUNK_SIZE, callback=_callback):
        while True:
            # ---- IDLE: wait for either trigger ----
            trigger = None
            first_chunk = None
            while trigger is None:
                chunk = audio_q.get()

                if _manual_toggle_event.is_set():
                    _manual_toggle_event.clear()
                    trigger = "manual"
                    first_chunk = chunk
                    break

                if _session_state.is_asleep():
                    continue  # wake-word disabled while asleep; manual still works above

                prediction = wake_model.predict(chunk)
                if any(score > WAKE_THRESHOLD for score in prediction.values()):
                    trigger = "wakeword"
                    first_chunk = None  # the wake word itself isn't part of the command

            # ---- RECORDING ----
            _notify("Voice Assistant", "Listening...")
            frames = [first_chunk] if first_chunk is not None else []
            start_time = time.monotonic()
            speech_detected = False
            silence_start: float | None = None

            while True:
                try:
                    chunk = audio_q.get(timeout=0.5)
                except queue.Empty:
                    chunk = None

                elapsed = time.monotonic() - start_time
                if elapsed > MAX_RECORDING_SECONDS:
                    break

                if chunk is None:
                    continue
                frames.append(chunk)

                if trigger == "manual":
                    if _manual_toggle_event.is_set():
                        _manual_toggle_event.clear()
                        break
                    continue

                # wake-word trigger: VAD-based turn detection
                score = vad.predict(chunk, frame_size=CHUNK_SIZE)
                if score > SPEECH_VAD_THRESHOLD:
                    speech_detected = True
                    silence_start = None
                elif speech_detected:
                    if silence_start is None:
                        silence_start = time.monotonic()
                    elif time.monotonic() - silence_start > SILENCE_STOP_SECONDS:
                        break
                elif elapsed > NO_SPEECH_TIMEOUT_SECONDS:
                    break  # wake word fired but nothing was ever said

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name
            _save_wav(frames, wav_path)
            _process_command(wav_path, layer1_available)


if __name__ == "__main__":
    main()
