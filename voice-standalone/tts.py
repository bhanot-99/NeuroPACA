"""
tts.py — spoken output for the voice assistant.
Supports:
- English: MeloTTS Indian-English voice ('EN_INDIA') by default (~0.15-0.20s per sentence),
  or Kokoro 'af_heart' (streaming) when configured via config.TTS_ENGLISH_VOICE = "kokoro".
- Hindi: Kokoro 'hf_alpha' voice (streaming).
- Punjabi: IndicF5 voice cloning model using local reference audio (~14.3s per phrase on CPU).
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
_indicf5_model = None

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PUNJABI_REF_AUDIO = os.path.join(_BASE_DIR, "tts_reference_audio", "PAN_F_HAPPY_00001.wav")
_PUNJABI_REF_TEXT_FILE = os.path.join(_BASE_DIR, "tts_reference_audio", "PAN_F_HAPPY_00001.txt")


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
    """Force-loads the configured English engine's model weights now,
    instead of on the first real speak() call. Reproduced live: the first
    MeloTTS call pays a ~20s cold-load penalty (vs. ~0.3s once warm) — on
    the critical path, that meant the daemon's very first spoken
    confirmation after every restart lagged ~20s behind its own desktop
    notification, and the main loop couldn't even listen for the next
    wake-word while it was loading. Call this once during startup, ideally
    in a background thread that runs alongside the other slow model loads
    (semantic matcher, wake-word) so the cost is hidden, not additive."""
    if getattr(config, "TTS_ENGLISH_VOICE", "melo") == "melo":
        _get_melo()
    else:
        _get_kokoro("af_heart")


def _get_indicf5():
    """Lazy loads IndicF5 for Punjabi voice generation."""
    global _indicf5_model
    if _indicf5_model is None:
        import torch
        import torchaudio
        import soundfile as sf
        from transformers.dynamic_module_utils import get_class_from_dynamic_module

        # Ensure torchaudio uses soundfile directly without requiring torchcodec
        def _safe_torchaudio_load(filepath, *args, **kwargs):
            data, sr = sf.read(filepath, dtype="float32")
            tensor = torch.from_numpy(data)
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0)
            else:
                tensor = tensor.t()
            return tensor, sr

        torchaudio.load = _safe_torchaudio_load

        repo_id = "raajain/IndicF5"
        INF5Model = get_class_from_dynamic_module(f"{repo_id}--model.INF5Model", repo_id)
        INF5Config = get_class_from_dynamic_module(f"{repo_id}--model.INF5Config", repo_id)
        config_obj = INF5Config(name_or_path=repo_id)

        # Force CPU device for IndicF5 to avoid CUDA OOM alongside Melo/Kokoro
        orig_cuda_avail = torch.cuda.is_available
        try:
            torch.cuda.is_available = lambda: False
            _indicf5_model = INF5Model(config_obj)
        finally:
            torch.cuda.is_available = orig_cuda_avail
    return _indicf5_model


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


def speak_punjabi(text: str) -> None:
    """Speaks Punjabi using IndicF5 voice cloning with reference sample."""
    text = clean_for_speech(text)
    if not text:
        return
    if not os.path.exists(_PUNJABI_REF_AUDIO) or not os.path.exists(_PUNJABI_REF_TEXT_FILE):
        return

    import numpy as np
    import soundfile as sf

    with open(_PUNJABI_REF_TEXT_FILE, "r", encoding="utf-8") as f:
        ref_text = f.read().strip()

    model = _get_indicf5()
    audio = model(text, ref_audio_path=_PUNJABI_REF_AUDIO, ref_text=ref_text)
    audio = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        sf.write(path, np.array(audio, dtype=np.float32), samplerate=24000)
        subprocess.run(["aplay", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def detect_lang(text: str) -> str:
    """Auto-detect language from script: Devanagari -> hi, Gurmukhi -> pa, default -> en."""
    for ch in text:
        if "\u0900" <= ch <= "\u097f":
            return "hi"
        if "\u0a00" <= ch <= "\u0a7f":
            return "pa"
    return "en"


def speak(text: str, lang: str | None = None) -> None:
    """Speak text out loud across English, Hindi, and Punjabi.
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

    if lang == "pa":
        if getattr(config, "TTS_ENABLE_PUNJABI", True):
            speak_punjabi(text)
        return

    if lang == "en" and getattr(config, "TTS_ENGLISH_VOICE", "melo") == "melo":
        speak_indian_english(text)
        return

    # Fallback / streaming path for English (Kokoro) and Hindi (Kokoro hf_alpha)
    target_voice = _VOICE_BY_LANG.get(lang, "af_heart")
    _, stream = _get_kokoro(target_voice)
    stream.feed(text)
    stream.play()
