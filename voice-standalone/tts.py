"""
tts.py — spoken output for the voice assistant.
Supports:
- English: MeloTTS Indian-English voice ('EN_INDIA') by default (~0.15-0.20s per sentence),
  or Kokoro 'af_heart' (streaming) when configured via config.TTS_ENGLISH_VOICE = "kokoro".
- Hindi: Kokoro 'hf_alpha' voice (streaming).

Punjabi (IndicF5 voice cloning) was built and benchmarked, then removed
before merge: live full-pipeline latency measured 96s per phrase on CPU —
far past the documented isolated benchmark (~55s) and unusable for a live
assistant. See ARCHITECTURE.md's Step 7 section for the full writeup.
"""

import os
import re
import subprocess
import tempfile

import config
from RealtimeTTS import TextToAudioStream, KokoroEngine

_VOICE_BY_LANG = {
    "en": "af_heart",
    "hi": "hf_alpha",
}

_kokoro_engine = None
_kokoro_stream = None
_kokoro_current_voice = None
_melo_model = None


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


def _get_kokoro(voice: str = "af_heart"):
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


def _get_melo():
    """Lazy loads MeloTTS for Indian English."""
    global _melo_model
    if _melo_model is None:
        from melo.api import TTS as MeloTTS
        _melo_model = MeloTTS(language="EN", device="auto")
    return _melo_model


def warm_up_english() -> None:
    """Force-loads the configured English engine now, instead of on the
    first real speak() call. For melo specifically this means running a
    real (silent, discarded) tts_to_file() call, not just constructing the
    model — found live (2026-09-16) that constructing alone was NOT
    enough: MeloTTS lazy-loads a separate BERT text-frontend on the first
    *actual* synthesis call, not at construction, so the original
    construct-only version of this function left that ~20s cost still
    sitting on the first real spoken response even after "warming up."
    Reproduced directly: a fresh process's first tts.speak() call, even
    after calling this function beforehand, still took ~36s end to end
    (~20s of that the frontend load, the rest real playback time) — this
    fix forces that load here instead, silently (no aplay call — nothing
    should audibly play just because the daemon started up). Call this
    once during startup, ideally in a background thread that runs
    alongside the other slow model loads (semantic matcher, wake-word) so
    the cost is hidden, not additive."""
    if getattr(config, "TTS_ENGLISH_VOICE", "melo") == "melo":
        model = _get_melo()
        speaker_ids = model.hps.data.spk2id
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            model.tts_to_file("Ready.", speaker_ids["EN_INDIA"], path, speed=0.95)
        finally:
            if os.path.exists(path):
                os.unlink(path)
    else:
        _get_kokoro("af_heart")


def speak_indian_english(text: str) -> None:
    """Speaks English with Indian accent using MeloTTS."""
    text = clean_for_speech(text)
    if not text:
        return
    model = _get_melo()
    speaker_ids = model.hps.data.spk2id
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        model.tts_to_file(text, speaker_ids["EN_INDIA"], path, speed=0.95)
        subprocess.run(["aplay", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def detect_lang(text: str) -> str:
    """Auto-detect language from script: Devanagari -> hi, default -> en."""
    for ch in text:
        if "\u0900" <= ch <= "\u097f":
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
        if detected != "en":
            lang = detected
        else:
            lang = "en"

    if lang == "en" and getattr(config, "TTS_ENGLISH_VOICE", "melo") == "melo":
        speak_indian_english(text)
        return

    # Fallback / streaming path for English (Kokoro) and Hindi (Kokoro hf_alpha)
    target_voice = _VOICE_BY_LANG.get(lang, "af_heart")
    _, stream = _get_kokoro(target_voice)
    stream.feed(text)
    stream.play()
