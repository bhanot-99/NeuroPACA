"""
Step 6 & Step 9 — static tier registry. Never inferred at runtime, so it's
predictable and auditable (see ARCHITECTURE.md's "Safety measures" section).

DANGEROUS contains the 11 skills marked with a specific 🔒 comment in actions.py
during Steps 1-4, plus logout added in Step 9:
  - shutdown, restart, logout: session-ending actions that close applications
    and can lose unsaved work. logout was promoted in Step 9 because terminating
    the graphical desktop session carries the same unsaved-loss risk.
  - force_quit, kill_process: kills arbitrary running processes.
  - rename_file, move_file, delete_file, empty_trash: destructive or structural
    filesystem mutations.
  - install_updates, restart_service: elevated system/package management.
  - run_terminal: arbitrary shell execution on the host system.

REVIEW contains skills with notable side effects or privacy considerations that
are recoverable but warrant a 2-second visible/spoken warning before executing:
  - clear_app_cache: deletes real data. Caches are regenerable by definition,
    so it isn't DANGEROUS, but it's not a pure read/toggle either.
  - extract_archive: can silently overwrite existing files at the destination
    if names collide — recoverable (the archive remains) but not risk-free.
  - copy_file: like extract_archive, shutil.copy2 can silently overwrite an
    existing file of the same name at the destination folder.
  - sleep: systemctl suspend halts all background processes and suspends the
    machine to RAM. Recoverable by pressing power/key, but warrants a 2s pause
    to catch accidental triggers or confusion with "sleep_stop_listening".
  - take_photo: accesses hardware webcam (/dev/video0) and writes images to
    disk. Hardware actuation with privacy implications warrants a notice.

Everything else is SAFE: read-only queries, toggles, app launching, web
searches, and media playback.
"""

DANGEROUS: frozenset[str] = frozenset({
    "shutdown", "restart", "logout", "force_quit", "rename_file", "move_file",
    "delete_file", "empty_trash", "kill_process", "install_updates",
    "restart_service", "run_terminal",
})

REVIEW: frozenset[str] = frozenset({
    "clear_app_cache", "extract_archive", "copy_file", "sleep", "take_photo",
})


def tier_of(skill_name: str) -> str:
    """Returns 'DANGEROUS', 'REVIEW', or 'SAFE' — SAFE is the default for
    anything not explicitly listed above, matching the doc's own framing
    (SAFE is the common case, the other two are the exceptions)."""
    if skill_name in DANGEROUS:
        return "DANGEROUS"
    if skill_name in REVIEW:
        return "REVIEW"
    return "SAFE"
