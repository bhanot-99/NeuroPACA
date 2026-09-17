"""
Category E — Filesystem & directory inspection skills.
Provides list_desktop_folders and filesystem inspection helpers.
"""

import re
from collections.abc import Callable

from skills.files import _match_list_desktop_folders

SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    ("list_desktop_folders", _match_list_desktop_folders),
]
