"""
tts.py — spoken output for the voice assistant.
- English: Piper (en_IN-spicor, NavGurukul AI Labs) — a dedicated Indian-
  English voice, 63.5MB, CPU-only, ~1.1s cold load / ~0.3s per sentence.
- Hindi: Kokoro (hf_alpha) — streaming via RealtimeTTS.

Two earlier engines were tried and dropped, not silently — see
ARCHITECTURE.md's Step 7 section for the full history:
- MeloTTS (EN_INDIA) was the original English voice: real Indian accent,
  but only one voice with no gender/tone alternate, and no pitch control
  exposed at synthesis time — when it didn't suit, there was nothing to
  switch to within the model itself. Its cold-load bug (a lazy-loaded BERT
  frontend, ~20s on the first real synthesis call) got fixed properly
  before this replacement — see git history — but the voice itself still
  couldn't be changed, which Piper's swap resolves at the root.
- Kokoro was tried for English too (9 real male + 9 female American
  voices) — good voices, zero Indian accent, which was the actual point.
  Kept for Hindi only, where it already had real, verified voices
  (hf_alpha/hf_beta female, hm_omega/hm_psi male).
- Punjabi (IndicF5) was built, benchmarked, and removed — too slow
  (96s/phrase full-pipeline) for live use. Also documented in
  ARCHITECTURE.md, not repeated here.

PROCESS ISOLATION, and why it's not optional: speak() and warm_up() run
the actual synthesis in a separate OS process (this same file, invoked as
a script), never in daemon.py's/main.py's own process. Found live
(2026-09-16), through extensive repeated testing against the real,
unmodified daemon.py entry point (not simplified reproductions, which
misleadingly kept succeeding): constructing an onnxruntime- or torch-based
TTS engine (Piper OR Kokoro, both hit it — not just the torch one) in the
same process as openwakeword's onnxruntime instance intermittently hung,
non-deterministically — sometimes fast, sometimes stuck 60s+ burning real
CPU with no error. Root cause understood at a class level (native ML
inference libraries independently claiming/configuring thread pools —
see _process_guard.py's docstring for the specific torch-bundled-libgomp
case that was isolated and confirmed) but a single environment-variable
fix could not be verified to eliminate it with full confidence in the
time available, given the non-determinism. Full process isolation sidesteps
the entire bug class at the architecture level instead of chasing each
library pairing: TTS's native libraries now never coexist in the same
process as openwakeword's or ctranslate2's, so there is nothing left to
conflict. Real, deliberate cost: each speak() call pays its own process-
spawn + cold-load time (Piper ~1.1s, Kokoro a few seconds) rather than
reusing a warm in-process model — acceptable given the existing "Let me
check" interim-phrase pattern already exists specifically to cover
multi-second response latency, and given a daemon that reliably reaches
"Ready" matters more than shaving a second off each spoken reply.
"""

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import wave

import config
from RealtimeTTS import TextToAudioStream, KokoroEngine

_HINDI_VOICE = "hf_alpha"

_kokoro_engine = None
_kokoro_stream = None
_kokoro_current_voice = None
_piper_voice = None

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPER_MODEL_PATH = os.path.join(_BASE_DIR, "piper_voices", "en_IN-spicor.onnx")


def clean_for_speech(text: str) -> str:
    """Prepares text for natural speech output by removing markdown, code blocks,
    URLs, and bullet formatting, and truncating overly long responses."""
    if not text or not text.strip():
        return ""
    # Omit multi-line code blocks
    text = re.sub(r"```[\s\S]*?```", "code block omitted", text)
    # Strip backticks from inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Replace URLs with simple word "link"
    text = re.sub(r"https?://\S+", "link", text)
    # Remove markdown bold and italic asterisks
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Remove bullet markers
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    # Remove markdown header hashes
    text = re.sub(r"^\s*#+\s+", "", text, flags=re.MULTILINE)
    # Collapse multiple whitespace/newlines
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate overly long text (spoken gist rather than full report dump)
    if len(text) > 250:
        truncated = text[:247]
        last_period = truncated.rfind(".")
        if last_period > 100:
            text = truncated[:last_period + 1]
        else:
            text = truncated + "..."
    return text


def _get_kokoro(voice: str):
    """Lazy loads Kokoro RealtimeTTS engine with calm natural speed (0.95)."""
    global _kokoro_engine, _kokoro_stream, _kokoro_current_voice
    if _kokoro_engine is None:
        _kokoro_engine = KokoroEngine(voice=voice, default_speed=0.95)
        _kokoro_stream = TextToAudioStream(_kokoro_engine)
        _kokoro_current_voice = voice
    elif _kokoro_current_voice != voice:
        _kokoro_engine.set_voice(voice)
        _kokoro_current_voice = voice
    return _kokoro_engine, _kokoro_stream


def _get_piper():
    """Lazy loads the Piper English voice. Requires piper_voices/en_IN-spicor.onnx
    + its .onnx.json — not committed to the repo (a 63.5MB binary weight
    file), see ARCHITECTURE.md's setup section for the download step."""
    global _piper_voice
    if _piper_voice is None:
        from piper import PiperVoice
        if not os.path.exists(_PIPER_MODEL_PATH):
            raise FileNotFoundError(
                f"Piper voice model not found at {_PIPER_MODEL_PATH} — "
                "see ARCHITECTURE.md's Step 7 setup section to download it."
            )
        _piper_voice = PiperVoice.load(_PIPER_MODEL_PATH)
    return _piper_voice


def _spawn_worker(argv: list[str], timeout: float = 25.0) -> int:
    """Spawns a worker process via os.posix_spawn — deliberately NOT
    subprocess.run/Popen (which uses fork()+exec() on POSIX; fork() in a
    multi-threaded process is a well-documented hazard — it only
    duplicates the calling thread, so if another thread held a lock at
    that instant, the child inherits it permanently locked). Also
    deliberately timeout-bounded, not just retried or trusted: found live
    (2026-09-16), through extensive repeated testing against the real,
    unmodified daemon.py, that TTS subprocess spawns hung intermittently,
    non-deterministically, even after switching to posix_spawn and after
    testing several other real fixes (LD_PRELOAD for a confirmed torch/
    onnxruntime bundled-libgomp conflict, reordering when this runs
    relative to other library loads) — each one fixed some reproductions
    but not the real entry point reliably enough to trust with full
    confidence in the time available. Rather than ship an unbounded call
    that *might* still hang for a cause not fully pinned down, this
    guarantees the daemon can never block forever on it: past `timeout`
    seconds the worker gets force-killed and this raises, which both
    call sites (warm_up() and daemon.py's `_speak`) already catch — so a
    stuck TTS call becomes "this one response didn't get spoken," not
    "the whole daemon never reaches Ready." An honest, deliberate
    trade-off: guaranteed daemon liveness over a fully explained root
    cause that further debugging time could not converge on."""
    pid = os.posix_spawn(argv[0], argv, os.environ.copy())
    deadline = time.monotonic() + timeout
    while True:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid == pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise TimeoutError(f"TTS worker timed out after {timeout}s: {argv[1:]}")
        time.sleep(0.05)


def warm_up() -> None:
    """Runs one silent, throwaway synthesis in the isolated worker process
    (see module docstring's PROCESS ISOLATION note) before the daemon
    claims "Ready". Doesn't make subsequent speak() calls faster — each
    one spawns its own fresh process with its own cold load — but does
    warm the OS page cache for Piper's model file, and turns a missing/
    broken model file into a loud startup failure instead of a silent one
    on someone's first real command."""
    exit_code = _spawn_worker([sys.executable, os.path.abspath(__file__), "--warm-worker"])
    if exit_code != 0:
        raise RuntimeError(f"TTS warm-up worker exited with code {exit_code}")


def speak_english(text: str) -> None:
    """Speaks English with an Indian accent using Piper."""
    text = clean_for_speech(text)
    if not text:
        return
    voice = _get_piper()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        with wave.open(path, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        subprocess.run(["aplay", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def detect_lang(text: str) -> str:
    """Auto-detect language from script: Devanagari -> hi, default -> en."""
    for ch in text:
        if "ऀ" <= ch <= "ॿ":
            return "hi"
    return "en"


def speak(text: str, lang: str | None = None) -> None:
    """Speak text out loud across English and Hindi. Blocks until speech
    finishes. Auto-detects language if not explicitly provided. Runs the
    actual synthesis in an isolated subprocess — see module docstring's
    PROCESS ISOLATION note for why this isn't just an in-process call."""
    text = clean_for_speech(text)
    if not text:
        return

    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"

    _spawn_worker([sys.executable, os.path.abspath(__file__), "--speak-worker", lang, text])


def _run_as_worker() -> None:
    """Entry point when this file is invoked as a script (by speak() and
    warm_up() above, in an isolated subprocess) rather than imported.
    Deliberately minimal — no error recovery beyond a clear stderr message
    and a non-zero exit, since the parent process is the one deciding what
    "TTS failed" should mean for the rest of the pipeline."""
    if len(sys.argv) >= 2 and sys.argv[1] == "--warm-worker":
        _get_piper()
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "--speak-worker":
        worker_lang, worker_text = sys.argv[2], sys.argv[3]
        if worker_lang == "hi":
            _, stream = _get_kokoro(_HINDI_VOICE)
            stream.feed(worker_text)
            stream.play()
        else:
            speak_english(worker_text)
        return
    print(f"[tts worker] unrecognized invocation: {sys.argv[1:]}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    try:
        _run_as_worker()
    except Exception as exc:
        print(f"[tts worker error] {exc}", file=sys.stderr)
        sys.exit(1)
