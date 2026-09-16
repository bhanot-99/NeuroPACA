"""
Layer-2 fallback: only reached once Layer 0 (regex) and Layer 1 (local
semantic match) have both already failed to resolve an utterance, and
config.LLM_FALLBACK_ENABLED is on.

Cascade, in order:
  1. Gemini (config.INTENT_MODEL, a flash-lite tier — checked directly
     against this API key's live model list, 2026-09-16: flash-lite is the
     smallest text-generation tier Google offers here, "Nano Banana" is
     image generation despite the name, and the Gemma models on this key
     are larger, not smaller). One call does double duty: pick a tool if
     the utterance is a command, or just answer directly in plain text if
     it's a genuine question — verified directly that the SDK reliably
     returns a text part instead of a function_call in that case, not
     assumed.
  2. On ANY Gemini failure (quota 429, a deprecated/removed model like the
     gemini-2.5-flash 404 this project already hit once, network issues —
     caught broadly on purpose, not just 429) — fall through to
     local_llm.py's Qwen2.5-1.5B cascade instead of just giving up.
     A 429 specifically also gets remembered via quota_tracker so the rest
     of today's utterances skip straight past Gemini instead of paying a
     network round-trip to fail again each time.

Benchmarked directly before choosing gemini-flash-lite-latest as the
primary and Qwen2.5-1.5B (not 3B — measured slower AND less accurate) as
the local fallback; see ARCHITECTURE.md for the full numbers.
"""

from google import genai
from google.genai import types
from google.genai import errors

import local_llm
import quota_tracker
from config import GEMINI_API_KEY, INTENT_MODEL

_client = genai.Client(api_key=GEMINI_API_KEY)

_TOOLS = [
    types.FunctionDeclaration(
        name="open_app",
        description="Launch a desktop application by name, e.g. firefox, code, spotify.",
        parameters={
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    ),
    types.FunctionDeclaration(
        name="web_search",
        description="Search Google or YouTube in the default browser.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "enum": ["google", "youtube"]},
            },
            "required": ["query", "site"],
        },
    ),
    types.FunctionDeclaration(
        name="set_volume",
        description="Set system output volume to an exact percentage (0-100).",
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    ),
    types.FunctionDeclaration(
        name="set_brightness",
        description="Set screen brightness to an exact percentage (0-100).",
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    ),
    types.FunctionDeclaration(
        name="run_terminal",
        description="Run an arbitrary shell command on the user's Linux machine.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    ),
    types.FunctionDeclaration(
        name="read_pdf",
        description="Read a PDF file by name from the user's common folders (Downloads, "
                     "Documents, Desktop, etc.) and return its extracted text.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
]

_TOOL = types.Tool(function_declarations=_TOOLS)

_SYSTEM_INSTRUCTION = (
    "You turn a spoken command into exactly one tool call when the command "
    "is an action. Pick the single best matching tool and fill its "
    "arguments from the command. If the command is instead a genuine "
    "factual or conversational question rather than an action, do NOT "
    "call any tool — just answer it directly in 1-2 short spoken "
    "sentences, conversational tone, no markdown. If it's neither an "
    "action nor a real question, do not call any tool and respond with "
    "nothing."
)


def _resolve_via_gemini(text: str) -> tuple[str | None, dict | None]:
    response = _client.models.generate_content(
        model=INTENT_MODEL,
        contents=[_SYSTEM_INSTRUCTION, text],
        config=types.GenerateContentConfig(tools=[_TOOL]),
    )
    for part in response.candidates[0].content.parts:
        if part.function_call:
            return part.function_call.name, dict(part.function_call.args)
        if part.text and part.text.strip():
            return "answer_question", {"question": text, "answer": part.text.strip()}
    return None, None


def _resolve_via_local(text: str) -> tuple[str | None, dict | None]:
    tool, args = local_llm.classify(text)
    if tool is None:
        return None, None
    if tool == "answer_question":
        ans = local_llm.answer(text)
        if ans is None:
            return None, None
        return "answer_question", {"question": text, "answer": ans}
    return tool, args


def resolve_intent(text: str) -> tuple[str | None, dict | None]:
    if not quota_tracker.is_exhausted_today():
        try:
            return _resolve_via_gemini(text)
        except errors.APIError as exc:
            if exc.code == 429:
                quota_tracker.mark_exhausted()
            # Any Gemini failure (quota, a deprecated model, network) falls
            # through to the local model rather than giving up — same
            # "degrade, don't dead-end" reasoning as the quota case.
    return _resolve_via_local(text)
