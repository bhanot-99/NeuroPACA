"""
Fast, free, zero-quota answer path for clean topic lookups ("what is X",
"who is X") — tried after Layer 0/1 have both already failed to match
anything, and BEFORE the Gemini/local LLM cascade in llm_intent.py.

Deliberately scoped narrow: only "what is/who is/what's/who's/tell me
about X" phrasing, not general questions ("why is the sky blue," "how
does X work") — those are causal/explanatory, not identity/definition
lookups, and a raw Wikipedia summary doesn't actually answer them well.
Anything outside this narrow shape, or that Wikipedia can't resolve
cleanly, returns None and falls through to the LLM cascade exactly as
before this existed — this only ever adds a path, never removes one.

Two real HTTP calls, not one: verified directly (2026-09-16) that fetching
the summary for the literal spoken topic is NOT reliable for common
single-word topics — "python" (the literal extraction from "what is
python") resolves to a disambiguation page ("Python may refer to..."),
not the programming-language article. Wikipedia's own search API's top
hit for "python", by contrast, correctly is "Python (programming
language)" — so this searches first, then fetches the summary for
whatever real article title that search returns, rather than guessing the
literal words are an exact title.
"""

import json
import re
import urllib.parse
import urllib.request

_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"

_TOPIC_PATTERN = re.compile(
    r"^\s*(?:what(?:'s| is)|who(?:'s| is)|tell me about)\s+(.+?)\s*\??\s*$",
    re.IGNORECASE,
)


def _extract_topic(text: str) -> str | None:
    m = _TOPIC_PATTERN.match(text)
    if not m:
        return None
    topic = m.group(1).strip()
    return topic or None


def _search_top_title(topic: str, timeout: float) -> str | None:
    params = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": topic,
        "format": "json", "srlimit": 1,
    })
    req = urllib.request.Request(f"{_SEARCH_URL}?{params}", headers={"User-Agent": "voice-standalone/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    results = data.get("query", {}).get("search", [])
    return results[0]["title"] if results else None


def _fetch_summary(title: str, timeout: float) -> tuple[str, str] | None:
    url = _SUMMARY_URL.format(urllib.parse.quote(title.replace(" ", "_")))
    req = urllib.request.Request(url, headers={"User-Agent": "voice-standalone/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if data.get("type") == "disambiguation":
        return None
    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    page_url = data.get("content_urls", {}).get("desktop", {}).get("page")
    return extract, (page_url or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")


def lookup(text: str, timeout: float = 4.0) -> tuple[str, str, str] | None:
    """Returns (question, answer, wikipedia_url) on a clean hit, or None —
    on ANY uncertainty (no phrasing match, no search hit, disambiguation,
    network failure) — never a guess, matching every other matcher's
    contract in this codebase."""
    topic = _extract_topic(text)
    if topic is None:
        return None
    try:
        title = _search_top_title(topic, timeout)
        if title is None:
            return None
        result = _fetch_summary(title, timeout)
    except Exception:
        return None
    if result is None:
        return None
    answer, url = result
    # Spoken-length cap, same discipline as tts.clean_for_speech — a full
    # Wikipedia summary paragraph is too long to speak as a quick answer.
    if len(answer) > 400:
        truncated = answer[:397]
        last_period = truncated.rfind(".")
        answer = truncated[:last_period + 1] if last_period > 100 else truncated + "..."
    return text, answer, url
