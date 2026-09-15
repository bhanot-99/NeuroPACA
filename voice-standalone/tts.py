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
"""

import os
import re
import subprocess
import tempfile
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


def warm_up() -> None:
    """Force-loads both engines now, instead of on the first real speak()
    call. Piper's cold load is already fast (~1.1s, measured directly) —
    this mainly matters for Kokoro/Hindi, whose model load is heavier.
    Call this once during startup, ideally in a background thread that
    runs alongside the other slow model loads (semantic matcher,
    wake-word) so the cost is hidden, not additive — same reasoning as
    every other warm-up in this codebase, just cheaper to pay now that
    Piper replaced MeloTTS's ~20s cold-load case."""
    _get_piper()
    _get_kokoro(_HINDI_VOICE)


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
    """Speak text out loud across English and Hindi.
    Blocks until speech finishes. Auto-detects language if not explicitly provided."""
    text = clean_for_speech(text)
    if not text:
        return

    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"

    if lang == "hi":
        _, stream = _get_kokoro(_HINDI_VOICE)
        stream.feed(text)
        stream.play()
        return

    speak_english(text)
