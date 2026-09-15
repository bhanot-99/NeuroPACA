"""
Generalized correction layer (Step 3) — see ARCHITECTURE.md's pipeline diagram.

"Never execute your best guess — correct it first" (Home Assistant's pattern,
credited in ARCHITECTURE.md's sources table). Any argument naming something we
have an authoritative local list for (installed apps today; known sites/files
in future steps) goes through this instead of trusting the raw transcription.

Algorithm, exactly as documented in the pipeline diagram:
  1. Substring match first — instant, zero ambiguity for unambiguous names.
  2. Else, an ensemble score: 0.6 x edit-distance-ratio + 0.4 x token-overlap.
  3. Accept only above the cutoff; otherwise return None — "unresolved," never
     a guess. The caller decides what happens next (fall through, etc.).

This replaces `skills/_app_resolver.py`'s previous plain `difflib.get_close_matches`
call, which only used a single edit-distance-style ratio with no token-overlap
signal — this generalizes it into the documented ensemble and makes it reusable
for whatever the next "real thing" argument turns out to be (Step 4).

Deliberately NOT applied to every argument that touches free text — see the
end of this file for which arguments were considered and excluded, and why.
"""

import difflib
import re


def _edit_distance_ratio(a: str, b: str) -> float:
    """Ratcliff/Obershelp similarity ratio, 0.0-1.0."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def token_overlap(a: str, b: str) -> float:
    """Jaccard token overlap, 0.0-1.0. Shared with skills/semantic_match.py
    (the same signal, same reasoning: only useful for close lexical variants,
    not genuine paraphrases that legitimately share few words)."""
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def resolve_against_known(
    spoken: str,
    candidates: list[tuple[str, str]],
    cutoff: float = 0.75,
) -> str | None:
    """Resolve spoken text against a list of known (match_key, return_value) pairs.

    match_key is what we compare the spoken text against (e.g. a lowercased
    display name); return_value is what's returned on a confident match (e.g.
    a canonical id, which may differ from match_key). Both may be the same
    string when there's no separate canonical id to preserve.

    Returns return_value on a confident match, or None — never a low-confidence
    guess.
    """
    if not spoken:
        return None

    # Pass 1: substring containment — ONE direction only: the spoken text
    # must be contained IN the candidate's full name (users say a shorter or
    # abbreviated form of the real name — "code" for "Visual Studio Code").
    # The reverse ("Files" contained in "cosmic files") is NOT safe: a short,
    # generic candidate name can be a coincidental substring of a longer,
    # more specific spoken phrase.
    #
    # Any containment match is a strong, unambiguous signal — that's what
    # lets a genuine short abbreviation ("brave" for "brave-browser") win
    # confidently without needing to separately clear the ensemble cutoff
    # below. But when MULTIPLE candidates satisfy containment, picking
    # whichever was enumerated first is a real bug found via live testing:
    # "settings" matched a 48-character desktop_id
    # (com.system76.cosmicsettings.legacyapplications) that happens to
    # contain the word, before ever considering the exact match "Settings"
    # (org.gnome.Settings) elsewhere in the list. Among all containment
    # hits, the SHORTEST match_key is the tightest, most specific one — an
    # exact match is the tightest possible — so that's the principled
    # tie-break, not enumeration order.
    substring_hits = [(key, value) for key, value in candidates if spoken in key]
    if substring_hits:
        _, return_value = min(substring_hits, key=lambda pair: len(pair[0]))
        return return_value

    # Pass 2: ensemble score, best match only, gated by cutoff.
    best_score = 0.0
    best_value: str | None = None
    for match_key, return_value in candidates:
        score = 0.6 * _edit_distance_ratio(spoken, match_key) + 0.4 * token_overlap(spoken, match_key)
        if score > best_score:
            best_score = score
            best_value = return_value

    if best_value is not None and best_score >= cutoff:
        return best_value
    return None


# ---------------------------------------------------------------------------
# Arguments considered for this correction layer, and why they're excluded —
# recorded here so a future reader knows this was a deliberate scoping
# decision, not something missed:
#
#   location (timezone_conversion) — actions.py's executor opens time.is/<city>
#     directly; that service already does its own lenient place-name matching
#     server-side. Our correction layer would need a complete local list of
#     every place time.is supports to avoid REJECTING valid inputs outside a
#     necessarily-incomplete list — that would make this skill worse, not
#     more correct. Left as free text on purpose.
#   query (google_search, youtube_search, wikipedia_search, duckduckgo_search,
#     unit_conversion) — arbitrary search text, not a named local entity we
#     have ground truth for. Nothing to correct against.
#   word (define_word, spell_word) — would need a real dictionary word list,
#     which doesn't exist in this codebase; a wrong word here costs an empty
#     search result, not a wrong action. Not worth adding a new dependency for.
#   expression (calculator) — free-form math text, not a named entity.
#   percent, state, number — numeric/enum values, already validated by range
#     checks or fixed vocabularies in their own extractors; nothing "fuzzy"
#     to resolve.
# ---------------------------------------------------------------------------
