"""
dispatch.py — the one place a resolved (skill name, args) pair actually
gets tier-checked, executed, and audited.

Factored out of main.py's inline loop and daemon.py's _process_command —
those two were already an acknowledged near-duplicate of each other (see
their own docstrings/confirm_loop.py's module docstring) before this file
existed. A third caller (live_conversation.py's Realtime tool-call bridge,
Step 8) needs the exact same tier-gating a live model's proposed action
must pass through, which made a fourth copy of this logic the wrong move —
this is the one chokepoint all three now share.

DANGEROUS-tier confirmation is inherently caller-specific (main.py blocks
on a terminal input() with no timeout, daemon.py waits up to 10s on a tray
click, live_conversation.py needs to report "waiting on you" back into a
live model turn instead of blocking) — so this module hands back the raw
confirm_and_run generator rather than trying to own that UI itself. Each
caller drives it to completion with finish_confirm() below once it has
gathered its own answer.
"""

import contextlib
import io
import time

import actions
import config
from confirm_loop import confirm_and_run
from skills import _audit, _session_state, _tiers


def execute_skill(name: str, args: dict, *, text: str, on_review=None) -> dict:
    """SAFE/REVIEW: executes immediately, audits, and returns
    {"outcome": "executed", "output": str}.

    DANGEROUS, with config.SAFETY_TIERS_ENABLED on: does NOT execute —
    returns {"outcome": "needs_confirmation", "generator": ..., "pause": ...}
    instead. The caller shows pause["preview"]/["warnings"]/["editable"],
    gathers its own answer, and calls finish_confirm() below to actually
    run it (or cancel it).

    REVIEW's "about to run" notice is caller-specific (a terminal print, a
    desktop notification, a spoken aside) the same way DANGEROUS's confirm
    UI is — on_review(name, args), when given, runs right before the 2s
    pause instead of this module guessing how to tell the user."""
    tier = _tiers.tier_of(name)

    if tier == "DANGEROUS" and config.SAFETY_TIERS_ENABLED:
        generator = confirm_and_run(name, args)
        pause = next(generator)
        return {"outcome": "needs_confirmation", "tier": tier, "generator": generator, "pause": pause}

    if tier == "REVIEW" and config.SAFETY_TIERS_ENABLED:
        if on_review is not None:
            on_review(name, args)
        time.sleep(2)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        actions.DISPATCH[name](args)
    output = buffer.getvalue()
    if output:
        _session_state.set_last_response(output)
    _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
    return {"outcome": "executed", "tier": tier, "output": output}


def finish_confirm(generator, sent, *, name: str, args: dict, text: str, tier: str) -> dict:
    """Resumes a generator returned by execute_skill()'s "needs_confirmation"
    result. `sent` is confirm_and_run's own protocol: None runs as-is, ""
    cancels, any other string is an edited replacement value."""
    try:
        generator.send(sent)
        result = None
    except StopIteration as done:
        result = done.value

    if result and result["cancelled"]:
        _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="cancelled")
        return {"outcome": "cancelled"}

    output = (result["output"] if result else "") or ""
    if output:
        _session_state.set_last_response(output)
    _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
    return {"outcome": "executed", "output": output}
