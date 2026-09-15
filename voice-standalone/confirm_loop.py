"""
The confirm-loop generator (Open Interpreter's pattern, adapted — see
ARCHITECTURE.md's "Safety measures" section and sources table). Used for
DANGEROUS-tier actions once config.SAFETY_TIERS_ENABLED is on.

The executor call is wrapped in a generator that pauses right before
running anything, yielding a preview + any machine-scan warnings. Whatever
is watching (main.py's terminal loop today; daemon.py's notification-based
confirm is a lighter, non-editable variant — see its own module) can edit
the relevant value while it's paused; on resume, the loop re-reads from
that edited value, not the original guess — an empty-string edit means
cancel, None means run as-is.

Usage:
    gen = confirm_and_run(skill_name, args)
    pause = next(gen)                     # {"preview": ..., "warnings": [...]}
    # show pause["preview"], let the user edit it, get back edited or None
    try:
        gen.send(edited)                  # None = run as-is, "" = cancel
    except StopIteration as done:
        result = done.value               # {"cancelled": bool, "output"/"reason": ...}
"""

import contextlib
import io

import actions
from skills import _machine_scan

# The single most relevant free-text argument per DANGEROUS skill — the one
# a misheard transcription is most likely to have gotten wrong, and the one
# worth letting the user fix without starting over. Skills with no natural
# free-text slot (shutdown, restart, empty_trash, install_updates) aren't
# listed — they get confirm/cancel only, nothing to edit.
_EDITABLE_ARG: dict[str, str] = {
    "run_terminal": "command",
    "delete_file": "name",
    "rename_file": "dest",
    "move_file": "dest",
    "force_quit": "app_name",
    "kill_process": "name",
    "restart_service": "service",
}


def preview_text(skill_name: str, args: dict) -> str:
    if skill_name == "run_terminal":
        return args.get("command", "")
    if not args:
        return skill_name
    return f"{skill_name}(" + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")"


def confirm_and_run(skill_name: str, args: dict):
    editable_key = _EDITABLE_ARG.get(skill_name)
    preview = preview_text(skill_name, args)
    warnings = _machine_scan.scan(preview)

    edited = yield {"preview": preview, "warnings": warnings, "editable": editable_key is not None}

    if edited == "":
        return {"cancelled": True}

    final_args = dict(args)
    if edited is not None and editable_key:
        final_args[editable_key] = edited

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        actions.DISPATCH[skill_name](final_args)
    return {"cancelled": False, "output": buffer.getvalue(), "args": final_args}
