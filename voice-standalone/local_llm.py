"""
Local LLM fallback for llm_intent.py's cascade — Qwen2.5-1.5B-Instruct via
Ollama (already running as a systemd service on this machine, already had
this exact model pulled). CPU-only on purpose: benchmarked directly
(2026-09-15/16) — Ollama defaults to 100% GPU offload, which shares the
same 4GB card Kokoro already uses; loading this model alone pushed VRAM
usage to ~2GB, real contention risk against TTS. Forced CPU
(`num_gpu: 0`) still answers in 400ms-2.5s, easily fast enough for a path
that only runs when Layer 0/1 AND Gemini have all already failed to
resolve something.

Split into two separate calls (classify, then answer) rather than one
combined call, because a single combined attempt was tested directly and
found unreliable: asked to both pick a tool AND write a real answer in one
JSON-constrained response, the model correctly identified "why is the sky
blue" needed an answer but then didn't reliably write one. Each call doing
one job, tested separately, worked cleanly both times — so that's what
shipped, not the more "elegant" one-call version that didn't actually work.

Known, accepted limitation: classification accuracy here is measurably
below Gemini's (5-way local benchmark: 5/5; 7-way with answer_question
added: 4/5 in a quick spot-check, "why is the sky blue" misclassified as
"none"). Acceptable because this only activates as the fallback-of-a-
fallback — Gemini is still the primary path every time quota allows it.
"""

import json
import urllib.request

MODEL = "qwen2.5:1.5b-instruct-q4_K_M"
_OLLAMA_URL = "http://localhost:11434/api/generate"

_TOOL_NAMES = [
    "open_app", "web_search", "set_volume", "set_brightness",
    "run_terminal", "answer_question", "none",
]

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": _TOOL_NAMES},
        "args": {"type": "object"},
    },
    "required": ["tool", "args"],
}

_CLASSIFY_PROMPT = """You turn a spoken command into a tool call. Available tools:
- open_app(app_name: string)
- web_search(query: string, site: "google"|"youtube")
- set_volume(percent: integer)
- set_brightness(percent: integer)
- run_terminal(command: string)
- answer_question() - use this when the user is asking a genuine factual/
  conversational question rather than giving a command. Leave args empty;
  the answer itself is generated separately.
If nothing matches any tool and it isn't a question either, use tool "none" with empty args.
Respond with JSON only: {{"tool": ..., "args": {{...}}}}

Command: {text}"""

_ANSWER_PROMPT = (
    "You are a voice assistant. Answer in 1-2 short spoken sentences, "
    "conversational tone, no markdown, no lists.\n\nQuestion: {text}"
)


def _call(prompt: str, fmt=None, timeout: float = 20.0) -> str:
    body = {"model": MODEL, "prompt": prompt, "stream": False, "options": {"num_gpu": 0}}
    if fmt is not None:
        body["format"] = fmt
    req = urllib.request.Request(
        _OLLAMA_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return (data.get("response") or "").strip()


def classify(text: str) -> tuple[str | None, dict | None]:
    """Returns (tool_name, args) — tool_name is one of _TOOL_NAMES, or
    (None, None) on any failure (Ollama down, bad JSON, unrecognized
    shape). Never guesses; a failure here just means the cascade has
    nothing left to try, same "None means I don't know" contract every
    other matcher in this codebase follows."""
    try:
        raw = _call(_CLASSIFY_PROMPT.format(text=text), fmt=_CLASSIFY_SCHEMA)
        parsed = json.loads(raw)
        tool = parsed.get("tool")
        args = parsed.get("args") or {}
        if tool not in _TOOL_NAMES or tool == "none":
            return None, None
        return tool, args
    except Exception:
        return None, None


def answer(text: str) -> str | None:
    """Generates a short spoken-style answer to a genuine question.
    Returns None on any failure rather than an empty/broken response."""
    try:
        result = _call(_ANSWER_PROMPT.format(text=text), timeout=30.0)
        return result or None
    except Exception:
        return None
