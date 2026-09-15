"""
skills/ package — Layer 0 deterministic grammar matching.

Exposes a single public function:

    match_skill(text: str) -> tuple[str | None, dict | None]

Returns (skill_name, kwargs) on a confident hit, (None, None) to fall through
to the LLM.  The scan order across categories matters:

  1. system_control.py  (A)  — volume/brightness first; very specific anchors
  2. app_window.py      (B)  — open/close/switch; app-name resolver applied
  3. files.py           (E)  — file/folder ops; MUST run before C1 — "search
                                my files for X" / "find a file called X" must
                                not be stolen by C1's much broader
                                "search for .+" -> google_search fallback
  4. web_knowledge.py   (C1) — web searches last; broadest patterns are here

Within each category file, more-specific patterns are listed before looser
ones (see the comments inside each module for the rationale).
"""

from skills.app_window import SKILLS as _B_SKILLS
from skills.devtools import SKILLS as _F_SKILLS
from skills.files import SKILLS as _E_SKILLS
from skills.system_control import SKILLS as _A_SKILLS
from skills.web_knowledge import SKILLS as _C1_SKILLS

# Flat ordered list: A → B → E → F → C1
# Category A and B have tight keyword anchors and run first; E's file-specific
# and F's dev-tool "search"/"check" phrasing must be checked before C1's
# broadest fallback (google_search) ever gets a chance to steal them.
_ALL_SKILLS = _A_SKILLS + _B_SKILLS + _E_SKILLS + _F_SKILLS + _C1_SKILLS


def match_skill(text: str) -> tuple[str | None, dict | None]:
    """Fast local Layer-0 scan against all known skills.

    Returns:
        (name, kwargs) if exactly one skill matched with full confidence.
        (None, None)   if no skill matched — caller should fall through to LLM.

    Never returns a low-confidence guess; a None means "I don't know," not
    "my best guess is X."
    """
    for name, matcher in _ALL_SKILLS:
        args = matcher(text)
        if args is not None:
            return name, args
    return None, None
