"""
Category L — System maintenance (6 skills, Step 4).

  L01  battery_health     — "how healthy is my battery" (capacity/wear,
                             distinct from A23 battery_status's charge %)
  L02  clear_app_cache    — "clear my cache"
  L03  check_uptime       — "how long has my computer been running"
  L04  list_startup_apps  — "what apps start automatically"
  L05  find_large_files   — "find large files on my computer"
  L06  system_info_summary — "give me a system info summary"
"""

import re
from collections.abc import Callable


def _match_battery_health(text: str) -> dict | None:
    if re.search(r"\bbattery\s+health\b|\bhow\s+healthy\s+is\s+my\s+battery\b"
                 r"|\bbattery\s+(?:wear|degradation|capacity)\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_clear_app_cache(text: str) -> dict | None:
    if re.search(r"\bclear\s+(?:my\s+)?(?:app\s+)?cache\b|\bclear\s+application\s+cache\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_check_uptime(text: str) -> dict | None:
    if re.search(r"\bcheck\s+(?:my\s+)?uptime\b|\bhow\s+long\s+has\s+(?:my\s+computer|this\s+machine)\s+been\s+(?:running|on|up)\b"
                 r"|\bwhat(?:'s|\s+is)\s+my\s+uptime\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_list_startup_apps(text: str) -> dict | None:
    if re.search(
        r"\bwhat\s+apps\s+start\s+automatically\b"
        r"|\blist\s+(?:my\s+)?startup\s+apps\b"
        r"|\bwhat(?:'s|\s+is)\s+in\s+my\s+startup\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_find_large_files(text: str) -> dict | None:
    if re.search(r"\bfind\s+large\s+files\b|\bwhat(?:'s|\s+is)\s+taking\s+up\s+(?:so\s+much\s+)?(?:disk\s+)?space\b",
                 text, re.IGNORECASE):
        return {}
    return None


def _match_system_info_summary(text: str) -> dict | None:
    if re.search(
        r"\b(?:give\s+me\s+a\s+)?system\s+info(?:rmation)?\s+summary\b"
        r"|\bwhat\s+(?:os|operating\s+system)\s+am\s+i\s+running\b"
        r"|\btell\s+me\s+about\s+my\s+system\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("battery_health", _match_battery_health),
    ("clear_app_cache", _match_clear_app_cache),
    ("check_uptime", _match_check_uptime),
    ("list_startup_apps", _match_list_startup_apps),
    ("find_large_files", _match_find_large_files),
    ("system_info_summary", _match_system_info_summary),
]
