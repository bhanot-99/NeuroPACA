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
import wiki_fastpath
from config import SAMPLE_RATE
from confirm_loop import confirm_and_run
from skills import _audit, _session_state, _tiers, semantic_match

try:
    import tts
except Exception as _tts_err:
    tts = None
    print(f"[warning] TTS unavailable: {_tts_err}")

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


def _notify(title: str, body: str, *, speak_text: str | None = None, speak: bool = True) -> None:
    """Every desktop notification also gets spoken, so nothing shown in the
    notification panel goes silent. speak_text overrides the body when the
    panel text (multi-line warnings, raw args dicts, etc.) isn't what you'd
    want read aloud; speak=False is for the one case where speaking would
    actively break things — see the "Listening..." call site."""
    import subprocess
    display_body = body.strip()
    if len(display_body) > 300:
        display_body = display_body[:300] + "…"
    subprocess.run(["notify-send", "--app-name=Voice Assistant", title, display_body], check=False)
    if speak:
        _speak(speak_text if speak_text is not None else body)


def _detect_lang(text: str) -> str:
    for ch in text:
        if "\u0900" <= ch <= "\u097f":
            return "hi"
    return "en"


def _clean_for_speech(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > 2:
        spoken = " ".join(lines[:2])
    else:
        spoken = " ".join(lines)
    if len(spoken) > 250:
        spoken = spoken[:247] + "..."
    return spoken


def _speak(text: str, lang: str | None = None) -> None:
    if tts is None:
        return
    text = _clean_for_speech(text)
    if not text:
        return
    if lang is None:
        lang = _detect_lang(text)
    try:
        tts.speak(text, lang=lang)
    except TypeError:
        try:
            tts.speak(text)
        except Exception as exc:
            print(f"[tts error] {exc}")
    except Exception as exc:
        print(f"[tts error] {exc}")


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
        _notify("Voice Assistant — error", f"Could not transcribe that: {exc}",
                 speak_text="Could not transcribe that audio.")
        return
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    if not text:
        _notify("Voice Assistant", "(heard nothing)", speak_text="I didn't hear anything.")
        return

    try:
        matched_layer = "layer0"
        name, args = skills.match_skill(text)
        if name is None and layer1_available:
            matched_layer = "layer1"
            name, args = semantic_match.match(text)
        if name is None:
            # Free, zero-quota, no API key — tried regardless of
            # LLM_FALLBACK_ENABLED since it isn't an LLM call at all, just
            # two HTTP GETs to Wikipedia's public API. Only ever narrows
            # what reaches the LLM cascade below, never replaces it: a
            # None here (no clean "what is X" phrasing, no search hit,
            # disambiguation, network failure) falls straight through.
            wiki_hit = wiki_fastpath.lookup(text)
            if wiki_hit is not None:
                matched_layer = "wiki"
                question, answer, url = wiki_hit
                name, args = "answer_question", {"question": question, "answer": answer, "url": url}
        if name is None and config.LLM_FALLBACK_ENABLED:
            matched_layer = "llm"
            name, args = llm_intent.resolve_intent(text)

        if name is None:
            _notify(f"Heard: {text}", "No matching action.")
            return

        if _session_state.is_asleep() and name != "wake_back_up":
            _notify("Voice Assistant", "(asleep — say 'wake up' to resume)",
                     speak_text="Asleep. Say wake up to resume.")
            return

        # Immediate interim phrase for slow actions or LLM resolution
        if matched_layer == "llm" or name in (
            "google_search", "youtube_search", "wikipedia_search",
            "duckduckgo_search", "speed_test", "define_word"
        ):
            _speak("Let me check.")

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
                speak_text=f"Please confirm: {pause['preview']}",
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
                _notify("Voice Assistant", "Cancelled (no confirmation within 10s).",
                         speak_text="Cancelled.")
                _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="cancelled")
                return
            output = (result["output"].strip() if result else "") or f"Ran: {name}"
            _session_state.set_last_response(output)
            _notify(f"Heard: {text}", output)
            _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
            return

        if tier == "REVIEW" and config.SAFETY_TIERS_ENABLED:
            _notify(f"About to run: {name}", str(args), speak_text=f"About to run {name}.")
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

    # Runs alongside the other slow model loads below rather than after —
    # MeloTTS's cold load (~20s, reproduced live) would otherwise sit on the
    # critical path of the very first spoken confirmation (even the "Ready"
    # message itself), during which the main loop can't listen for the next
    # wake-word either. Joined right before "Ready" so both TTS and the mic
    # are actually ready by the time that message fires.
    tts_warm_thread = None
    if tts is not None:
        def _warm_tts() -> None:
            try:
                tts.warm_up_english()
            except Exception as exc:
                print(f"[warning] TTS warm-up failed: {exc}")
        tts_warm_thread = threading.Thread(target=_warm_tts, daemon=True)
        tts_warm_thread.start()

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

    if tts_warm_thread is not None:
        print("Waiting for TTS warm-up to finish...")
        tts_warm_thread.join()

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
            # speak=False here on purpose: this notification fires right as
            # the VAD-based recording loop below starts consuming live mic
            # frames. Speaking through the speakers at this exact moment
            # would risk the TTS audio itself bleeding into the mic (no
            # acoustic echo cancellation in this pipeline) and either
            # getting transcribed as part of the command or falsely
            # tripping the VAD's speech_detected state.
            _notify("Voice Assistant", "Listening...", speak=False)
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

            # Found via a real user report: _process_command (STT + skill
            # resolution + execution) takes real wall-clock seconds, during
            # which the InputStream callback keeps stuffing fresh chunks
            # into audio_q — nothing is reading them meanwhile. Returning
            # straight to idle would burn through that whole backlog in a
            # tight burst against the wake-word model, including whatever
            # played *during* processing (e.g. the video the command itself
            # just opened) — a spurious "Listening..." right after a correct
            # action, with nothing real said. Drain it so idle only ever
            # scores live audio going forward.
            while not audio_q.empty():
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    break


if __name__ == "__main__":
    main()
