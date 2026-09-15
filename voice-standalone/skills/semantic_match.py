"""
Layer 1 — local semantic match (see ARCHITECTURE.md's decision pipeline).

Only runs when Layer 0 (grammar/regex) finds no match. Encodes the utterance
and every skill's example phrases into vectors with a local embedding model
(fastembed / BAAI/bge-small-en-v1.5 — benchmarked live against
sentence-transformers/all-MiniLM in Step 2; fastembed's ~1.3s warm startup
beat all-MiniLM's ~15s with equivalent ~12ms per-utterance latency), then
picks the skill whose example phrases are most similar by cosine similarity.

Two-threshold decision rule (from the pipeline diagram):
  score >= HIGH_THRESHOLD                    → accept immediately
  LOW_THRESHOLD <= score < HIGH_THRESHOLD     → blend with a token-overlap
                                                 signal, recheck against
                                                 HIGH_THRESHOLD
  score < LOW_THRESHOLD                       → reject, fall through to LLM

Never returns a low-confidence guess — None means "not sure," not "best guess."

Argument extraction: Layer 0's regexes are tightly coupled to their own exact
trigger wording (that's what makes them fast and precise). Layer 1 only runs
on phrasing Layer 0 already missed, so its argument extraction is deliberately
looser — a small set of shared, generic extractors (by argument *shape*, not
one per skill) rather than 50 bespoke ones.
"""

import re
from collections.abc import Callable

from skills._app_resolver import resolve_app_name

# ---------------------------------------------------------------------------
# Tunable thresholds — see ARCHITECTURE.md open question #1. Starting values
# from the pipeline diagram; tuned against real test utterances in Step 2.
# ---------------------------------------------------------------------------

# Tuned against real, independently-worded test utterances (smoke_test_semantic.py):
# correct-and-unambiguous matches scored 0.71-0.93, so 0.82 rejected genuine
# paraphrases outright. 0.75 keeps every measured false-accept-free while
# recovering most of the false rejects. The handful of remaining failures
# were genuine argmax collisions between semantically close skills (e.g.
# volume_up outscoring volume_down) that no threshold value fixes — those
# needed better, more distinguishing example phrases instead (see below).
HIGH_THRESHOLD = 0.75
LOW_THRESHOLD = 0.55

_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# ---------------------------------------------------------------------------
# Example phrases per skill — deliberately paraphrased, NOT copies of Layer
# 0's exact trigger wording. If these matched Layer 0's regex too, they'd
# never reach this layer in the real pipeline, and Layer 1 would never be
# exercised by anything meaningful.
# ---------------------------------------------------------------------------

SKILL_EXAMPLES: dict[str, list[str]] = {
    # --- Category A: System & power control ---
    "set_volume": ["can you set the volume to 40 percent", "make the sound level 65"],
    "volume_up": ["can you crank up the sound a bit", "make it louder please", "pump up the volume"],
    "volume_down": ["can you decrease the volume a bit", "lower the sound please", "bring the volume level down some", "that's too loud, turn it down"],
    "mute": ["can you silence the audio", "kill the sound for a second"],
    "unmute": ["bring the sound back", "unsilence the audio"],
    "set_brightness": ["make the screen brightness 30 percent", "set the display to 70"],
    "brightness_up": ["it's too dark in here, brighten the screen", "make the display brighter"],
    "brightness_down": ["this screen is really bright, tone it down", "dim it a little"],
    "toggle_dark_mode": ["switch over to the dark theme", "can we go back to light mode"],
    "lock_screen": ["I'm stepping away, lock it", "secure the screen while I'm gone", "I'm leaving my desk for a bit, lock the screen"],
    "shutdown": ["power the machine off completely", "shut this computer down"],
    "restart": ["can you reboot the machine", "restart this computer for me"],
    "logout": ["sign me out of this session", "end my session"],
    "sleep": ["put the machine to sleep", "suspend the system for now"],
    "screenshot": ["grab a picture of what's on screen", "capture what I'm looking at"],
    "start_recording": ["start capturing my screen", "begin recording what I'm doing"],
    "stop_recording": ["okay stop capturing now", "end the screen recording"],
    "toggle_display": ["mirror this to the external monitor", "extend my display to the second screen"],
    "toggle_wifi": ["can you switch the wireless on", "kill the wireless connection", "get my wifi connected"],
    "toggle_bluetooth": ["turn bluetooth off for me", "get my bluetooth radio going"],
    "toggle_airplane": ["put the machine in flight mode", "turn off airplane mode"],
    "toggle_dnd": ["stop interrupting me with notifications", "let notifications through again"],
    "toggle_night_light": ["turn on the warm screen filter", "switch off the blue light filter"],
    "battery_status": ["how much charge is left", "what's my battery looking like"],
    "toggle_kbd_backlight": ["light up the keyboard", "turn off the keyboard lighting"],
    "toggle_mic_mute": ["cut my microphone", "let my mic through again", "put my mic on mute", "mute my microphone"],

    # --- Category B: App & window management ---
    "switch_workspace": ["jump over to workspace three", "take me to desktop two"],
    "switch_to_app": ["bring firefox to the front", "my code editor is already open, bring it forward", "switch over to my already-running browser"],
    "close_app": ["shut the browser window", "get rid of the terminal window"],
    "force_quit": ["that app is frozen, kill it", "the browser is stuck, force it closed"],
    "list_open_apps": ["what do I currently have open", "show me my running programs"],
    "show_desktop": ["clear everything off my screen", "get all the windows out of the way"],
    "maximize_window": ["make this window take up the whole screen", "fill the screen with this window"],
    "reopen_last_closed": ["bring back the window I just closed", "undo closing that app"],
    "open_launcher": ["show me all my apps", "bring up the app grid"],
    "open_app": ["can you start the file manager for me", "launch my mail application", "launch a fresh text editor window"],

    # --- Category C1: Web search & general knowledge ---
    "google_search": ["look up the best pasta recipe", "find information about black holes"],
    "youtube_search": ["find me a lo-fi music video", "pull up a tutorial video on knitting"],
    "wikipedia_search": ["what does the encyclopedia say about Napoleon", "look up quantum physics on wiki"],
    "duckduckgo_search": ["use duck duck go to find a vpn comparison", "privacy search for open source alternatives"],
    "define_word": ["what does ephemeral actually mean", "give me the definition of ubiquitous", "I don't know this word, can you explain what it means"],
    "spell_word": ["can you spell out necessary for me", "how do you write the word occasion"],
    "get_date": ["what day of the week is it", "what's today's calendar date"],
    "get_time": ["got the time handy", "what time do we have right now"],
    "timezone_conversion": ["what time would it be in Sydney right now", "current time over in Berlin", "what's the clock reading in Tokyo", "tell me the time in New York"],
    "unit_conversion": ["how many miles is 10 kilometers", "convert 5 pounds into kilograms"],
    "calculator": ["what's 340 divided by 4", "add up 18 and 27 for me"],
    "my_ip_address": ["what address does my network show for me", "check my public ip"],
    "speed_test": ["how fast is my internet right now", "check my connection speed"],
    "today_in_history": ["anything notable happen on this date before", "give me a historical fact for today"],
}

# ---------------------------------------------------------------------------
# Argument extraction — shared, generic extractors keyed by argument shape.
# ---------------------------------------------------------------------------

_STATE_VOCAB = {"on": "on", "enable": "on", "enabled": "on",
                "off": "off", "disable": "off", "disabled": "off"}


def _no_args(_text: str) -> dict:
    return {}


def _percent_arg(text: str) -> dict | None:
    m = re.search(r"\b(\d{1,3})\s*(?:%|percent)?\b", text)
    if not m:
        return None
    percent = int(m.group(1))
    if not (0 <= percent <= 100):
        return None
    return {"percent": percent}


def _state_on_off(text: str) -> dict:
    for word, state in _STATE_VOCAB.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            return {"state": state}
    return {"state": None}


def _state_mute_unmute(text: str) -> dict:
    if re.search(r"\bunmute\b|\bunsilence\b|\bback\b|\bthrough\b", text, re.IGNORECASE):
        return {"state": "unmute"}
    if re.search(r"\bmute\b|\bsilence\b|\bcut\b|\boff\b", text, re.IGNORECASE):
        return {"state": "mute"}
    return {"state": None}


def _dark_light(text: str) -> dict:
    return {"dark": "light" not in text.lower()}


def _mirror_extend(text: str) -> dict:
    mode = None
    if re.search(r"\bmirror\b|\bduplicate\b", text, re.IGNORECASE):
        mode = "mirror"
    elif re.search(r"\bextend\b|\bsecond\s+screen\b", text, re.IGNORECASE):
        mode = "extend"
    return {"mode": mode}


_APP_CUE_WORDS = re.compile(
    r"\b(?:open|close|shut|quit|exit|kill|force\s+quit|force\s+close|start|launch|"
    r"get|bring|pull\s+up|switch\s+to|go\s+to|focus\s+on|focus|the|my|for\s+me|"
    r"to\s+the\s+front|window|app|application|program|editor|client|manager)\b",
    re.IGNORECASE,
)


def _app_name_trailing(text: str) -> dict | None:
    stripped = _APP_CUE_WORDS.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .?!")
    if not stripped:
        return None
    desktop_id = resolve_app_name(stripped.lower())
    if desktop_id is None:
        return None
    return {"app_name": desktop_id}


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    # Ordinals — "my second desktop" is just as natural as "desktop two."
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def _workspace_number(text: str) -> dict | None:
    m = re.search(r"\b(\d+)\b", text)
    if m:
        return {"number": int(m.group(1))}
    for word, num in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            return {"number": num}
    return None


_QUERY_CUE_WORDS = re.compile(
    r"\b(?:can\s+you|could\s+you|please|look\s+up|find|search|pull\s+up|get\s+me|"
    r"use|for|me|a|an|the|on|about|information|video|tutorial)\b",
    re.IGNORECASE,
)


def _trailing_query(text: str) -> dict | None:
    stripped = _QUERY_CUE_WORDS.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .?!")
    if not stripped:
        return None
    return {"query": stripped}


_WORD_CUE_WORDS = re.compile(
    r"\b(?:what|does|is|the|definition|meaning|of|actually|mean|can\s+you|"
    r"spell|out|write|give\s+me|how\s+do\s+you)\b",
    re.IGNORECASE,
)


def _trailing_word(text: str) -> dict | None:
    stripped = _WORD_CUE_WORDS.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .?!")
    if not stripped:
        return None
    return {"word": stripped}


_LOCATION_CUE_WORDS = re.compile(
    r"\b(?:what|time|would|it|be|in|current|right|now|over|do\s+we\s+have|got\s+the)\b",
    re.IGNORECASE,
)


def _trailing_location(text: str) -> dict | None:
    stripped = _LOCATION_CUE_WORDS.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .?!")
    if not stripped:
        return None
    return {"location": stripped}


def _whole_text_query(text: str) -> dict:
    return {"query": text.strip()}


_EXPR_CUE_WORDS = re.compile(
    r"\b(?:what's|what\s+is|calculate|compute|add\s+up|for\s+me)\b", re.IGNORECASE
)


def _trailing_expression(text: str) -> dict | None:
    stripped = _EXPR_CUE_WORDS.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .?!")
    if not stripped:
        return None
    return {"expression": stripped}


SKILL_EXTRACTOR: dict[str, Callable[[str], dict | None]] = {
    "set_volume": _percent_arg, "set_brightness": _percent_arg,
    "toggle_wifi": _state_on_off, "toggle_bluetooth": _state_on_off,
    "toggle_dnd": _state_on_off, "toggle_airplane": _state_on_off,
    "toggle_night_light": _state_on_off, "toggle_kbd_backlight": _state_on_off,
    "toggle_mic_mute": _state_mute_unmute,
    "toggle_dark_mode": _dark_light,
    "toggle_display": _mirror_extend,
    "switch_to_app": _app_name_trailing, "close_app": _app_name_trailing,
    "force_quit": _app_name_trailing, "open_app": _app_name_trailing,
    "switch_workspace": _workspace_number,
    "google_search": _trailing_query, "youtube_search": _trailing_query,
    "wikipedia_search": _trailing_query, "duckduckgo_search": _trailing_query,
    "define_word": _trailing_word, "spell_word": _trailing_word,
    "timezone_conversion": _trailing_location,
    "unit_conversion": _whole_text_query,
    "calculator": _trailing_expression,
}


def _extract_args(skill_name: str, text: str) -> dict | None:
    extractor = SKILL_EXTRACTOR.get(skill_name, _no_args)
    return extractor(text)


# ---------------------------------------------------------------------------
# Embedding model + precomputed example matrix — lazy singleton, built once.
# ---------------------------------------------------------------------------

_model = None
_skill_names: list[str] = []          # parallel to _example_vectors' skill index
_example_vectors = None               # shape: (num_examples, dim), L2-normalized
_example_skill_index: list[int] = []  # example i belongs to skill _skill_names[_example_skill_index[i]]
_example_texts: list[str] = []        # example i's raw text (for token-overlap blend)


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model


def _normalize(vectors):
    import numpy as np
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _ensure_index_built() -> None:
    global _skill_names, _example_vectors, _example_skill_index, _example_texts
    if _example_vectors is not None:
        return

    import numpy as np

    model = _get_model()
    _skill_names = list(SKILL_EXAMPLES.keys())
    all_texts: list[str] = []
    skill_index: list[int] = []
    for i, name in enumerate(_skill_names):
        for phrase in SKILL_EXAMPLES[name]:
            all_texts.append(phrase)
            skill_index.append(i)

    vectors = np.array(list(model.embed(all_texts)))
    _example_vectors = _normalize(vectors)
    _example_skill_index = skill_index
    _example_texts = all_texts


def _token_overlap(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match(text: str) -> tuple[str | None, dict | None]:
    """Layer 1 semantic match. Returns (name, args) or (None, None)."""
    import numpy as np

    _ensure_index_built()
    model = _get_model()

    query_vec = _normalize(np.array(list(model.embed([text]))))[0]
    sims = _example_vectors @ query_vec  # cosine similarity, all vectors normalized

    # Best similarity PER SKILL (a skill may have multiple example phrases).
    best_per_skill: dict[int, tuple[float, int]] = {}
    for example_idx, skill_idx in enumerate(_example_skill_index):
        score = float(sims[example_idx])
        if skill_idx not in best_per_skill or score > best_per_skill[skill_idx][0]:
            best_per_skill[skill_idx] = (score, example_idx)

    if not best_per_skill:
        return None, None

    best_skill_idx, (best_score, best_example_idx) = max(
        best_per_skill.items(), key=lambda kv: kv[1][0]
    )
    skill_name = _skill_names[best_skill_idx]

    if best_score >= HIGH_THRESHOLD:
        args = _extract_args(skill_name, text)
        return (skill_name, args) if args is not None else (None, None)

    if LOW_THRESHOLD <= best_score < HIGH_THRESHOLD:
        # A genuine paraphrase has LOW lexical overlap with its example phrase
        # by definition (different words, same meaning) — a naive weighted
        # average would penalize exactly the cases Layer 1 exists to catch.
        # Overlap can only ever help (rescue a close lexical near-miss),
        # never hurt a real paraphrase that simply shares no words.
        overlap = _token_overlap(text, _example_texts[best_example_idx])
        blended = max(best_score, 0.7 * best_score + 0.3 * overlap)
        if blended >= HIGH_THRESHOLD:
            args = _extract_args(skill_name, text)
            return (skill_name, args) if args is not None else (None, None)
        return None, None

    return None, None
