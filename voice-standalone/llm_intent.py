"""
llm_intent.py — Multi-Provider LLM Failover Cascade & Intent Resolution Router.

Layer-2 fallback: only reached once Layer 0 (regex) and Layer 1 (local
semantic match) have both already failed to resolve an utterance, and
config.LLM_FALLBACK_ENABLED is on.

Cascade Architecture (Strict Order):
  1. Primary: Google Gemini API (gemini-flash-lite-latest / gemini-2.5-flash)
  2. Secondary Fallback: Groq Cloud API (llama-3.3-70b-versatile / qwen-2.5-coder-32b)
  3. Tertiary Fallback: NVIDIA NIM API (nvidia/nemotron-3.5-lightning-30b-a3b)
  4. Ultimate Local Fallback (100% Offline): Local Ollama instance (qwen2.5:3b-instruct / 1.5b)

Central Security Chokepoint:
  All tool calls proposed by any provider return a standardized (tool_name, args) tuple
  which is fed directly into dispatch.execute_skill, ensuring policy classification
  (SAFE / REVIEW / DANGEROUS) and audit logging are strictly enforced.
"""

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import errors, types

import config
import local_llm
import quota_tracker
from config import (
    GEMINI_API_KEY,
    GROQ_API_KEY,
    GROQ_MODEL,
    INTENT_MODEL,
    NVIDIA_API_KEY,
    NVIDIA_MODEL,
)

# Shared tool schema definitions
_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "open_app",
        "description": "Launch a desktop application by name, e.g. firefox, code, spotify.",
        "parameters": {
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    },
    {
        "name": "web_search",
        "description": "Search Google or YouTube in the default browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "enum": ["google", "youtube"]},
            },
            "required": ["query", "site"],
        },
    },
    {
        "name": "set_volume",
        "description": "Set system output volume to an exact percentage (0-100).",
        "parameters": {
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    },
    {
        "name": "set_brightness",
        "description": "Set screen brightness to an exact percentage (0-100).",
        "parameters": {
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    },
    {
        "name": "run_terminal",
        "description": "Run an arbitrary shell command on the user's Linux machine.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_pdf",
        "description": "Read a PDF file by name from the user's common folders (Downloads, Documents, Desktop, etc.) and return its extracted text.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "read_latest_emails",
        "description": "Read or search email messages from inbox, filter by sender or query, or read full content of a specific email by index.",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of recent emails to read (default 5)"},
                "sender": {"type": "string", "description": "Filter emails by sender name or address"},
                "query": {"type": "string", "description": "Search query or keyword within email content"},
                "index": {"type": "integer", "description": "1-based index of a specific email to read in full (e.g. 1 for latest, 2 for 2nd recent)"},
            },
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to a specified recipient with a subject and message body via SMTP.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "The message body text to send"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "list_desktop_folders",
        "description": "List the folders and directories located on the user's Desktop (~/Desktop).",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
]

# Google Gemini tool declarations
_GEMINI_TOOLS = [
    types.FunctionDeclaration(
        name=s["name"],
        description=s["description"],
        parameters=s["parameters"],
    )
    for s in _TOOL_SPECS
]
_GEMINI_TOOL_CONFIG = types.Tool(function_declarations=_GEMINI_TOOLS)

# OpenAI-compatible tool specifications (Groq, NVIDIA NIM)
_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["parameters"],
        },
    }
    for s in _TOOL_SPECS
]

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

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ===========================================================================
# ─── Provider 1: Google Gemini API (Primary) ───────────────────────────────
# ===========================================================================

def _resolve_via_gemini(text: str) -> Tuple[Optional[str], Optional[dict]]:
    if _client is None:
        return None, None
    response = _client.models.generate_content(
        model=INTENT_MODEL,
        contents=[_SYSTEM_INSTRUCTION, text],
        config=types.GenerateContentConfig(tools=[_GEMINI_TOOL_CONFIG]),
    )
    if not response.candidates or not response.candidates[0].content:
        return None, None

    for part in response.candidates[0].content.parts:
        if part.function_call:
            return part.function_call.name, dict(part.function_call.args or {})
        if part.text and part.text.strip():
            return "answer_question", {"question": text, "answer": part.text.strip()}
    return None, None


# ===========================================================================
# ─── Generic OpenAI-Compatible Caller (Groq / NVIDIA NIM) ───────────────────
# ===========================================================================

def _call_openai_compatible_api(
    url: str,
    api_key: str,
    model: str,
    text: str,
    provider: str,
    timeout: float = 12.0,
) -> Tuple[Optional[str], Optional[dict]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": text},
        ],
        "tools": _OPENAI_TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            quota_tracker.mark_exhausted(provider, f"429 Rate Limit: {exc.reason}")
        elif exc.code >= 500:
            quota_tracker.mark_exhausted(provider, f"{exc.code} Server Error")
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        quota_tracker.mark_exhausted(provider, f"Network/Timeout: {exc}")
        raise

    choices = data.get("choices", [])
    if not choices:
        return None, None
    msg = choices[0].get("message", {})
    tool_calls = msg.get("tool_calls")
    if tool_calls and len(tool_calls) > 0:
        tc = tool_calls[0]
        fn = tc.get("function", {})
        fname = fn.get("name")
        raw_args = fn.get("arguments", "{}")
        try:
            fargs = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except Exception:
            fargs = {}
        return fname, fargs

    content = msg.get("content", "").strip()
    if content:
        return "answer_question", {"question": text, "answer": content}
    return None, None


# ===========================================================================
# ─── Provider 2: Groq Cloud API (Secondary Fallback) ───────────────────────
# ===========================================================================

def _resolve_via_groq(text: str) -> Tuple[Optional[str], Optional[dict]]:
    if not GROQ_API_KEY:
        return None, None
    if quota_tracker.is_exhausted("groq"):
        return None, None
    return _call_openai_compatible_api(
        url="https://api.groq.com/openai/v1/chat/completions",
        api_key=GROQ_API_KEY,
        model=GROQ_MODEL,
        text=text,
        provider="groq",
    )


# ===========================================================================
# ─── Provider 3: NVIDIA NIM API (Tertiary Fallback) ────────────────────────
# ===========================================================================

def _resolve_via_nvidia(text: str) -> Tuple[Optional[str], Optional[dict]]:
    if not NVIDIA_API_KEY:
        return None, None
    if quota_tracker.is_exhausted("nvidia"):
        return None, None
    return _call_openai_compatible_api(
        url="https://integrate.api.nvidia.com/v1/chat/completions",
        api_key=NVIDIA_API_KEY,
        model=NVIDIA_MODEL,
        text=text,
        provider="nvidia",
    )


# ===========================================================================
# ─── Provider 4: Local Ollama (Ultimate Offline Fallback) ───────────────────
# ===========================================================================

def _resolve_via_local(text: str) -> Tuple[Optional[str], Optional[dict]]:
    tool, args = local_llm.classify(text)
    if tool is None:
        return None, None
    if tool == "answer_question":
        ans = local_llm.answer(text)
        if ans is None:
            return None, None
        return "answer_question", {"question": text, "answer": ans}
    return tool, args


# ===========================================================================
# ─── Router Cascade ────────────────────────────────────────────────────────
# ===========================================================================

def resolve_intent(text: str) -> Tuple[Optional[str], Optional[dict]]:
    """Resolves an utterance through the multi-provider cascade with automatic failover.
    Order: Gemini -> Groq -> NVIDIA NIM -> Local Ollama (Qwen2.5-3B)."""
    # 1. Primary: Google Gemini API
    if not quota_tracker.is_exhausted("gemini") and GEMINI_API_KEY:
        try:
            name, args = _resolve_via_gemini(text)
            if name is not None:
                return name, args
        except errors.APIError as exc:
            if exc.code == 429:
                quota_tracker.mark_exhausted("gemini", str(exc))
        except Exception as exc:
            quota_tracker.mark_exhausted("gemini", str(exc))

    # 2. Secondary Fallback: Groq Cloud API
    try:
        name, args = _resolve_via_groq(text)
        if name is not None:
            return name, args
    except Exception as exc:
        quota_tracker.mark_exhausted("groq", str(exc))

    # 3. Tertiary Fallback: NVIDIA NIM API
    try:
        name, args = _resolve_via_nvidia(text)
        if name is not None:
            return name, args
    except Exception as exc:
        quota_tracker.mark_exhausted("nvidia", str(exc))

    # 4. Ultimate Local Fallback (100% Offline): Local Ollama
    return _resolve_via_local(text)


# ===========================================================================
# ─── Intent Completeness & Silence Endpointing Analysis ───────────────────
# ===========================================================================

# Punctuation pattern for trailing ellipses or dangling comma
_TRAILING_PUNCT_REGEX = re.compile(r"(\.{2,}|…|,)\s*$")

# Conjunctions, prepositions, and hesitation markers that indicate incomplete utterance
_TRAILING_CONJUNCTIONS = {
    "and", "or", "um", "uh", "er", "ah", "with", "to", "then", "like", "also", "plus"
}


def is_trailing_utterance(text: str) -> bool:
    """Returns True if the transcribed query appears incomplete or trailing.

    Detects:
      - Trailing hesitations: "um", "uh", "er", "ah"
      - Dangling conjunctions: "and", "or", "then", "plus", "also"
      - Dangling prepositions: "with", "to"
      - Trailing ellipsis (...) or comma (,)
    """
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if _TRAILING_PUNCT_REGEX.search(stripped):
        return True
    cleaned = re.sub(r"[^\w\s]", "", stripped).strip().lower()
    words = cleaned.split()
    if not words:
        return False
    return words[-1] in _TRAILING_CONJUNCTIONS


def clean_trailing_utterance(text: str) -> str:
    """Strips trailing punctuation, conjunctions, and hesitations from an utterance."""
    if not text or not isinstance(text, str):
        return ""
    cur = text.strip()
    cur = _TRAILING_PUNCT_REGEX.sub("", cur).strip()
    words = cur.split()
    while words:
        clean_last = re.sub(r"[^\w]", "", words[-1]).lower()
        if clean_last in _TRAILING_CONJUNCTIONS:
            words.pop()
        else:
            break
    return " ".join(words).strip()


def check_intent_completeness(text: str) -> Dict[str, Any]:
    """Analyzes a transcribed query for completion status.

    Returns:
      - is_complete (bool): True if finished and ready to dispatch immediately.
      - trailing (bool): True if utterance ends in trailing conjunctions/hesitations.
      - clean_text (str): Utterance with trailing incomplete elements stripped.
    """
    trailing = is_trailing_utterance(text)
    clean = clean_trailing_utterance(text) if trailing else (text.strip() if text else "")
    return {
        "is_complete": not trailing,
        "trailing": trailing,
        "clean_text": clean,
    }

