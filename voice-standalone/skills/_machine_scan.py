"""
Machine-scan tripwire (Open Interpreter's safe-mode pattern, adapted — see
guide.md's sources table). A cheap pattern check on a resolved
command, run before showing any confirmation preview — catches an obviously
destructive command even on a quick skim, rather than relying entirely on
the human reading carefully.

This does NOT decide whether a command is gated — run_terminal (and the
other 🔒 skills) are DANGEROUS by static tier assignment regardless of what
this finds. It only decides how loudly the confirm-loop should warn within
that gate, exactly as guide.md specifies.
"""

import re

_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f\b|\brm\s+-[a-z]*f[a-z]*r\b", re.IGNORECASE), "recursive force-delete (rm -rf)"),
    (re.compile(r"\bdd\s+if=", re.IGNORECASE), "raw disk write (dd)"),
    (re.compile(r"\bmkfs\b", re.IGNORECASE), "filesystem format (mkfs)"),
    (re.compile(r">\s*/dev/(?:sd|nvme|hd)", re.IGNORECASE), "direct write to a disk device"),
    (re.compile(r"\bcurl\b.*\|\s*(?:sh|bash)\b|\bwget\b.*\|\s*(?:sh|bash)\b", re.IGNORECASE), "piping a download straight into a shell"),
    (re.compile(r"\bsudo\b|\bpkexec\b", re.IGNORECASE), "runs with elevated privileges"),
    (re.compile(r"\bkill\s+-9\b|\bkill\s+-(?:sigkill|SIGKILL)\b", re.IGNORECASE), "unconditional kill -9"),
    (re.compile(r":\(\)\s*\{.*\};\s*:", re.IGNORECASE), "fork bomb pattern"),
    (re.compile(r"\bchmod\s+-R\s+777\b", re.IGNORECASE), "recursive world-writable permissions"),
]


def scan(command: str) -> list[str]:
    """Returns a list of human-readable warnings for every dangerous pattern
    found in `command` — empty list if none match."""
    return [description for pattern, description in _DANGEROUS_PATTERNS if pattern.search(command)]
