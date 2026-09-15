"""
Category C1 — Web search & general knowledge (14 unmarked skills only).

Skills with ⚙️ tags (Wolfram Alpha, weather, forecast, news, movie info,
currency conversion, stock price, translate) are NOT implemented here —
they need external API accounts and belong in Step 4.

Implemented (14 unmarked skills):
  C01  google_search       — "search for X", "google X"
  C02  youtube_search      — "search YouTube for X", "find X on YouTube"
  C03  wikipedia_search    — "Wikipedia X", "look up X on Wikipedia"
  C04  duckduckgo_search   — "DuckDuckGo X", "search DuckDuckGo for X"
  C05  define_word         — "define X", "what does X mean"
  C06  spell_word          — "how do you spell X", "spell X"
  C07  get_date            — "what's the date", "what day is it"
  C08  get_time            — "what time is it", "current time"
  C09  timezone_conversion — "what time is it in Tokyo"
  C10  unit_conversion     — "convert 5 km to miles"
  C11  calculator          — "what is 42 times 7", "calculate 100 / 4"
  C12  my_ip_address       — "what's my IP", "what is my IP address"
  C13  speed_test          — "run a speed test", "test my internet speed"
  C14  today_in_history    — "what happened today in history"

Order rationale: YouTube/Wikipedia/DuckDuckGo matchers run before google_search
because google_search has a broad "search for .+" fallback that would otherwise
consume "search for X on YouTube" before the youtube matcher sees it.
define_word runs before spell_word (different anchor words, no collision risk).
calculator runs before unit_conversion (different trigger words, low risk).
"""

import re
from collections.abc import Callable


# ---------------------------------------------------------------------------
# C02 — youtube_search  (before google_search — see order note above)
# ---------------------------------------------------------------------------

def _match_youtube_search(text: str) -> dict | None:
    m = re.search(
        r"\b(?:search|find|look\s+up|play)\s+(?:for\s+)?(.+?)\s+on\s+youtube\b"
        r"|\byoutube\s+(?:search\s+(?:for\s+)?|for\s+)(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    query = (m.group(1) or m.group(2) or "").strip().rstrip(".")
    if not query:
        return None
    return {"query": query}


# ---------------------------------------------------------------------------
# C03 — wikipedia_search  (before google_search)
# ---------------------------------------------------------------------------

def _match_wikipedia_search(text: str) -> dict | None:
    m = re.search(
        r"\bwikipedia\s+(?:search\s+(?:for\s+)?|for\s+|article\s+(?:about\s+|on\s+))?(.+)"
        r"|\b(?:search|look\s+up|find)\s+(.+?)\s+on\s+wikipedia\b"
        r"|\bwiki\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().rstrip(".")
    if not query:
        return None
    return {"query": query}


# ---------------------------------------------------------------------------
# C04 — duckduckgo_search  (before google_search)
# ---------------------------------------------------------------------------

def _match_duckduckgo_search(text: str) -> dict | None:
    m = re.search(
        r"\b(?:search|find|look\s+up)\s+(?:for\s+)?(.+?)\s+on\s+duck\s*duck\s*go\b"
        r"|\bduck\s*duck\s*go\s+(?:search\s+(?:for\s+)?|for\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().rstrip(".")
    if not query:
        return None
    return {"query": query}


# ---------------------------------------------------------------------------
# C05 — define_word
# ---------------------------------------------------------------------------

def _match_define_word(text: str) -> dict | None:
    m = re.search(
        r"\bdefine\s+(?:the\s+word\s+)?(.+)"
        r"|\bwhat\s+(?:does|is\s+the\s+(?:definition|meaning)\s+of)\s+(.+?)\s+mean\b"
        r"|\bmeaning\s+of\s+(?:the\s+word\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    word = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not word:
        return None
    return {"word": word}


# ---------------------------------------------------------------------------
# C06 — spell_word
# ---------------------------------------------------------------------------

def _match_spell_word(text: str) -> dict | None:
    m = re.search(
        r"\b(?:how\s+do\s+you\s+spell|spell\s+(?:the\s+word\s+)?|spelling\s+of\s+(?:the\s+word\s+)?)(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    word = m.group(1).strip().rstrip("?.")
    if not word:
        return None
    return {"word": word}


# ---------------------------------------------------------------------------
# C07 — get_date
# ---------------------------------------------------------------------------

def _match_get_date(text: str) -> dict | None:
    if re.search(
        r"\bwhat(?:'s|\s+is)\s+(?:today(?:'s|\s+)?)?(?:the\s+)?date\b"
        r"|\bwhat\s+(?:day|date)\s+is\s+(?:it\s+)?today\b"
        r"|\btoday(?:'s)?\s+date\b"
        r"|\bwhat\s+day\s+is\s+(?:it\s+)?today\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# C08 — get_time
# (runs after get_date to avoid "what day/date" matching here)
# ---------------------------------------------------------------------------

def _match_get_time(text: str) -> dict | None:
    if re.search(
        r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?time\b"
        r"|\bwhat\s+time\s+is\s+it\b"
        r"|\bcurrent\s+time\b"
        r"|\btell\s+me\s+the\s+time\b",
        text, re.IGNORECASE
    ):
        # Exclude timezone-qualified queries → those go to timezone_conversion
        if not re.search(r"\bin\s+\w+|\bfor\s+\w+\s+timezone\b", text, re.IGNORECASE):
            return {}
    return None


# ---------------------------------------------------------------------------
# C09 — timezone_conversion
# ---------------------------------------------------------------------------

def _match_timezone_conversion(text: str) -> dict | None:
    m = re.search(
        # "what time is it in Tokyo" / "what's the time in London"
        r"\bwhat\s+time\s+is\s+it\s+in\s+(.+?)(?:\s*\??\s*$)"
        r"|\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?time\s+in\s+(.+?)(?:\s*\??\s*$)"
        r"|\btime\s+in\s+(.+?)(?:\s*\??\s*$)"
        r"|\bconvert\s+time\s+(?:to|for)\s+(.+?)(?:\s*\??\s*$)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    location = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not location:
        return None
    return {"location": location}


# ---------------------------------------------------------------------------
# C10 — unit_conversion
# ---------------------------------------------------------------------------

_UNIT_KEYWORDS = (
    r"km|kilometers?|miles?|meters?|feet|foot|inches?|centimeters?|cm|yards?"
    r"|kilograms?|kg|pounds?|lbs?|grams?|ounces?|oz"
    r"|liters?|gallons?|milliliters?|ml|fl\s*oz"
    r"|celsius|fahrenheit|kelvin"
    r"|mph|kph|m/s|knots?"
)

def _match_unit_conversion(text: str) -> dict | None:
    m = re.search(
        r"\bconvert\s+(.+?)\s+(?:to|into|in)\s+(" + _UNIT_KEYWORDS + r")\b"
        r"|\b(\d+(?:\.\d+)?)\s+(" + _UNIT_KEYWORDS + r")\s+(?:to|into|in)\s+(" + _UNIT_KEYWORDS + r")\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    return {"query": text.strip()}


# ---------------------------------------------------------------------------
# C11 — calculator
# ---------------------------------------------------------------------------

def _match_calculator(text: str) -> dict | None:
    # Reject immediately if the text starts with a volume/brightness/system keyword —
    # these are category-A skills that may slip through if their value is out-of-range.
    if re.search(
        r"^\s*(?:set\s+)?(?:volume|brightness|screen\s+brightness)\b",
        text, re.IGNORECASE
    ):
        return None

    m = re.search(
        r"\b(?:calculate|compute|what(?:'s|\s+is))\s+(.+?)(?:\s*\??\s*$)"
        r"|\bwhat(?:'s|\s+is)\s+(\d.+?)(?:\s*\??\s*$)"
        # Bare arithmetic expression: must contain at least one operator (+−*/^%)
        r"|\b(\d[\d\s]*[+\-*/^%][\d\s+\-*/^().%]*)(?:\s*\??\s*$)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    expr = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    # Must contain at least one operator or the word "times/divided/plus/minus"
    if not re.search(r"[+\-*/^%]|times|divided|plus|minus|squared|cubed|percent|power|root", expr, re.IGNORECASE):
        return None
    # Reject pure date/time questions that slipped through "what's X"
    if re.search(r"\btime\b|\bdate\b|\bday\b|\bweather\b", expr, re.IGNORECASE):
        return None
    return {"expression": expr}


# ---------------------------------------------------------------------------
# C12 — my_ip_address
# ---------------------------------------------------------------------------

def _match_my_ip_address(text: str) -> dict | None:
    if re.search(
        r"\bmy\s+(?:ip|ip\s+address|external\s+ip|public\s+ip)\b"
        r"|\bwhat(?:'s|\s+is)\s+my\s+ip\b"
        r"|\bwhat\s+is\s+my\s+(?:ip|ip\s+address|public\s+ip)\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# C13 — speed_test
# ---------------------------------------------------------------------------

def _match_speed_test(text: str) -> dict | None:
    if re.search(
        r"\b(?:run|do|start|check|test)\s+(?:a\s+)?(?:internet\s+)?speed\s+test\b"
        r"|\bspeed\s+test\b"
        r"|\btest\s+(?:my\s+)?(?:internet|network|connection)\s+speed\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# C14 — today_in_history
# ---------------------------------------------------------------------------

def _match_today_in_history(text: str) -> dict | None:
    if re.search(
        r"\b(?:what\s+happened\s+)?today\s+in\s+history\b"
        r"|\bhistorical\s+(?:events?\s+)?(?:for|on)\s+today\b"
        r"|\bon\s+this\s+day\s+in\s+history\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


# ---------------------------------------------------------------------------
# C01 — google_search  (broadest pattern → last in registry)
# ---------------------------------------------------------------------------

def _match_google_search(text: str) -> dict | None:
    m = re.search(
        r"\b(?:google|search(?:\s+for)?)\s+(.+)"
        r"|\bsearch\s+(?:the\s+)?(?:web|internet|online)\s+(?:for\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    query = next((g for g in m.groups() if g), "").strip().rstrip(".")
    if not query:
        return None
    # Don't steal queries already claimed by specific-site matchers — they've
    # already run before this point in the scan order below.
    return {"query": query}


# ---------------------------------------------------------------------------
# Ordered skill registry.
# Specific site matchers (YouTube, Wikipedia, DuckDuckGo) run BEFORE the
# broad google_search to avoid the "search for X on YouTube" false-capture bug.
# ---------------------------------------------------------------------------

SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    # Site-specific searches first
    ("youtube_search", _match_youtube_search),
    ("wikipedia_search", _match_wikipedia_search),
    ("duckduckgo_search", _match_duckduckgo_search),
    # Knowledge / lookup
    ("define_word", _match_define_word),
    ("spell_word", _match_spell_word),
    # Date/time
    ("get_date", _match_get_date),
    ("timezone_conversion", _match_timezone_conversion),  # before get_time
    ("get_time", _match_get_time),
    # Calculation / conversion
    ("calculator", _match_calculator),
    ("unit_conversion", _match_unit_conversion),
    # Network / info
    ("my_ip_address", _match_my_ip_address),
    ("speed_test", _match_speed_test),
    ("today_in_history", _match_today_in_history),
    # Broadest pattern last
    ("google_search", _match_google_search),
]
