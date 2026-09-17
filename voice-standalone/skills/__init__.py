"""
skills/ package — Layer 0 deterministic grammar matching.

Exposes a single public function:

    match_skill(text: str) -> tuple[str | None, dict | None]

Returns (skill_name, kwargs) on a confident hit, (None, None) to fall through
to the LLM.  The scan order across categories matters:

  1. assistant_meta.py  (J)  — sleep/wake/cancel run first: meta-commands
                                about the assistant itself should win over
                                any other interpretation
  2. system_control.py  (A)  — volume/brightness; very specific anchors
  3. app_window.py      (B)  — open/close/switch; app-name resolver applied
  4. files.py           (E)  — file/folder ops; MUST run before C1 — "search
                                my files for X" / "find a file called X" must
                                not be stolen by C1's much broader
                                "search for .+" -> google_search fallback
  5. devtools.py        (F)  — dev-tool checks; same reasoning as E
  6. media.py           (D)  — "play X on Y" playback phrasing
  7. productivity.py    (G)  — minimal test to-do only
  8. maintenance.py     (L)  — system maintenance queries
  9. web_knowledge.py   (C1) — web searches last; broadest patterns are here

Within each category file, more-specific patterns are listed before looser
ones (see the comments inside each module for the rationale).
"""

from skills.app_window import SKILLS as _B_SKILLS
from skills.assistant_meta import SKILLS as _J_SKILLS
from skills.communication import SKILLS as _H_SKILLS
from skills.devtools import SKILLS as _F_SKILLS
from skills.files import SKILLS as _E_SKILLS
from skills.maintenance import SKILLS as _L_SKILLS
from skills.media import SKILLS as _D_SKILLS
from skills.productivity import SKILLS as _G_SKILLS
from skills.system_control import SKILLS as _A_SKILLS
from skills.web_knowledge import SKILLS as _C1_SKILLS

# Flat ordered list: J → A → B → E → F → D → G → H → L → C1
# J's meta-commands run first (should win over any other interpretation);
# A and B have tight keyword anchors and run early; E's file-specific and
# F's dev-tool "search"/"check" phrasing must be checked before C1's broadest
# fallback (google_search) ever gets a chance to steal them. D/G/H/L placed
# before C1 too and verified empirically, not assumed, to be collision-free.
_ALL_SKILLS = (
    _J_SKILLS + _A_SKILLS + _B_SKILLS + _E_SKILLS + _F_SKILLS
    + _D_SKILLS + _G_SKILLS + _H_SKILLS + _L_SKILLS + _C1_SKILLS
)


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
