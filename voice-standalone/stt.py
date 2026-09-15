"""Local speech-to-text via faster-whisper (CPU, int8).

Runs offline so every voice command — including ones that resolve to a
fully local skill like set_brightness — no longer depends on the Gemini
free-tier quota (20 requests/day) just to get transcribed. Model size is
"base" multilingual — benchmarked at ~1.1s per command on this machine
(16 cores, no usable CUDA) vs. "tiny"'s ~0.6s, a deliberate tradeoff for
better real-world accuracy (noise/accent/distance) the user chose over
staying at tiny's speed. "small" was also benchmarked (~3.7s) and rejected
as too slow. The LLM-intent fallback in llm_intent.py still uses Gemini for
phrases the local skill matcher can't parse, but that's now the only
per-command cloud dependency, not STT.
"""

from faster_whisper import WhisperModel

_model = WhisperModel("base", device="cpu", compute_type="int8")


def transcribe(wav_path: str) -> str:
    # condition_on_previous_text=False: default True feeds each generated
    # segment back in as context for the next one, which on short/unclear
    # command audio can spiral into a runaway repeat loop (reproduced live:
    # "we will be the one who will be the one who will be..."). Turning it
    # off benchmarked equal-or-faster (no context-conditioning overhead) AND
    # fixed the loop — a strict improvement, not a speed/accuracy tradeoff.
    segments, _info = _model.transcribe(
        wav_path, beam_size=1, condition_on_previous_text=False
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
