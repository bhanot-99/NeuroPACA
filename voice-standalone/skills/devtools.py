"""
Category F — Terminal & dev tools (13 new skills, Step 4; run_terminal
already exists from Phase 0 and isn't duplicated here).

  F02  git_status               — "git status", "what's the git status"
  F03  check_processes          — "list running processes"
  F04  kill_process       🔒   — "kill the process named X" (raw process
                                   name, distinct from B08 force_quit's
                                   app-resolver-based approach — this is
                                   for scripts/daemons, not GUI apps)
  F05  check_port                — "what's using port 8080"
  F06  ping_host                 — "ping google.com"
  F07  check_cpu                 — "what's my cpu usage"
  F08  check_memory               — "how much memory am I using"
  F09  check_system_load          — "what's my system load"
  F10  open_terminal_at            — "open a terminal at downloads"
  F11  check_package_version       — "what version of git do I have"
  F12  check_for_updates           — "check for updates"
  F13  install_updates       🔒   — "install updates", "update my system"
  F14  restart_service       🔒   — "restart the nginx service"
"""

import re
from collections.abc import Callable


def _match_git_status(text: str) -> dict | None:
    if re.search(r"\bgit\s+status\b|\bwhat(?:'s|\s+is)\s+(?:the\s+)?git\s+status\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_check_processes(text: str) -> dict | None:
    if re.search(
        r"\blist\s+(?:running\s+)?processes\b"
        r"|\bwhat\s+processes\s+are\s+running\b"
        r"|\bshow\s+me\s+running\s+processes\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_kill_process(text: str) -> dict | None:
    m = re.search(
        r"\bkill\s+(?:the\s+)?process\s+(?:named|called)?\s*(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = m.group(1).strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_check_port(text: str) -> dict | None:
    m = re.search(
        r"\bwhat(?:'s|\s+is)\s+using\s+port\s+(\d+)"
        r"|\bcheck\s+port\s+(\d+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    port = next((g for g in m.groups() if g), "")
    if not port:
        return None
    return {"port": int(port)}


def _match_ping_host(text: str) -> dict | None:
    m = re.search(r"\bping\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    host = m.group(1).strip().rstrip("?.")
    if not host:
        return None
    return {"host": host}


def _match_check_cpu(text: str) -> dict | None:
    if re.search(r"\b(?:what(?:'s|\s+is)\s+my\s+)?cpu\s+usage\b|\bcheck\s+cpu\s+usage\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_check_memory(text: str) -> dict | None:
    if re.search(
        r"\bhow\s+much\s+memory\s+am\s+i\s+using\b"
        r"|\bcheck\s+memory\s+usage\b"
        r"|\bmemory\s+usage\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_check_system_load(text: str) -> dict | None:
    if re.search(r"\b(?:what(?:'s|\s+is)\s+my\s+)?system\s+load\b|\bcheck\s+system\s+load\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_open_terminal_at(text: str) -> dict | None:
    m = re.search(
        r"\bopen\s+(?:a\s+)?terminal\s+(?:at|in)\s+(?:my\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    folder = m.group(1).strip().rstrip("?.")
    if not folder:
        return None
    return {"folder": folder}


def _match_check_package_version(text: str) -> dict | None:
    m = re.search(
        r"\bwhat\s+version\s+of\s+(.+?)\s+do\s+i\s+have\b"
        r"|\bcheck\s+the\s+version\s+of\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    package = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not package:
        return None
    return {"package": package}


def _match_check_for_updates(text: str) -> dict | None:
    if re.search(r"\bcheck\s+for\s+updates\b|\bare\s+there\s+any\s+updates\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_install_updates(text: str) -> dict | None:
    if re.search(
        r"\binstall\s+(?:the\s+)?updates\b"
        r"|\bupdate\s+my\s+system\b"
        r"|\bupdate\s+(?:all\s+)?(?:my\s+)?packages\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_restart_service(text: str) -> dict | None:
    m = re.search(r"\brestart\s+(?:the\s+)?(.+?)\s+service\b", text, re.IGNORECASE)
    if not m:
        return None
    service = m.group(1).strip().rstrip("?.")
    if not service:
        return None
    return {"service": service}


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("git_status", _match_git_status),
    ("check_processes", _match_check_processes),
    ("kill_process", _match_kill_process),
    ("check_port", _match_check_port),
    ("ping_host", _match_ping_host),
    ("check_cpu", _match_check_cpu),
    ("check_memory", _match_check_memory),
    ("check_system_load", _match_check_system_load),
    ("open_terminal_at", _match_open_terminal_at),
    ("check_package_version", _match_check_package_version),
    ("check_for_updates", _match_check_for_updates),
    ("install_updates", _match_install_updates),
    ("restart_service", _match_restart_service),
]
