"""
Step 6 — static tier registry. Never inferred at runtime, so it's
predictable and auditable (see ARCHITECTURE.md's "Safety measures" section).

DANGEROUS here is exactly the 11 skills already marked with a specific,
individually-reasoned 🔒 comment in actions.py during Steps 1-4 (each with
its own "why," not a blanket rule) — this registry doesn't re-litigate
those calls, it collects them into one place a dispatcher can query.

Two additions beyond the original 🔒 set, each independently justified
rather than mechanically applied:
  - clear_app_cache -> REVIEW: deletes real data. Caches are meant to be
    regenerable by definition, so it isn't DANGEROUS, but it's not a pure
    read/toggle either — worth a beat of visibility before it runs.
  - extract_archive -> REVIEW: can silently overwrite existing files at the
    destination if names collide — genuinely recoverable (the archive still
    exists) but not risk-free the way opening a file is.

Everything else is SAFE: read-only queries, toggles, app launching, web
searches, and media playback. Tiers stay entirely inert until Step 6's
activation flag (config.SAFETY_TIERS_ENABLED) is turned on — that switch is
explicitly the user's call, not this file's.
"""

DANGEROUS: frozenset[str] = frozenset({
    "shutdown", "restart", "force_quit", "rename_file", "move_file",
    "delete_file", "empty_trash", "kill_process", "install_updates",
    "restart_service", "run_terminal",
})

REVIEW: frozenset[str] = frozenset({
    "clear_app_cache", "extract_archive",
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
