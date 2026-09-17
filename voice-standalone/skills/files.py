"""
Category E — Files & filesystem (17 skills, Step 4 + Step 8's read_pdf).

  E01  find_file            — "find a file called X", "locate X on my computer"
  E02  open_file             — "open the file X"
  E03  open_folder           — "open my downloads folder"
  E04  list_files            — "list files in downloads"
  E05  create_folder         — "create a folder called X"
  E06  rename_file      🔒   — "rename X to Y"
  E07  copy_file              — "copy X to Y"
  E08  move_file        🔒   — "move X to Y"
  E09  delete_file      🔒   — "delete the file X"
  E10  compress_archive       — "compress X into a zip"
  E11  extract_archive        — "extract X"
  E12  check_disk_space       — "how much disk space do I have"
  E13  empty_trash      🔒   — "empty the trash"
  E14  open_recent_downloads  — "open my recent downloads"
  E15  search_file_contents   — "search my files for X"
  E16  open_file_manager_at   — "open file manager at downloads"
  E17  read_pdf               — "read my resume pdf"

Placed BEFORE web_knowledge.py (C1) in skills/__init__.py's scan order: E15's
"search my files for X" and E01's "find a file called X" must not be stolen
by C1's much broader "search for .+" -> google_search fallback. Verified via
smoke test, not assumed.
"""

import re
from collections.abc import Callable

from skills._paths import resolve_folder


def _match_find_file(text: str) -> dict | None:
    m = re.search(
        r"\bfind\s+(?:a\s+|the\s+)?file\s+(?:called|named)?\s*(.+)"
        r"|\blocate\s+(?:the\s+file\s+)?(.+?)\s+on\s+my\s+(?:computer|machine|system)\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_open_file(text: str) -> dict | None:
    m = re.search(r"\bopen\s+(?:the\s+)?file\s+(?:called|named)?\s*(.+)", text, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_open_folder(text: str) -> dict | None:
    m = re.search(
        r"\bopen\s+(?:my\s+|the\s+)?(.+?)\s+folder\b"
        r"|\bopen\s+(?:my\s+|the\s+)?folder\s+(?:called|named)\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"folder": name}


def _match_list_files(text: str) -> dict | None:
    m = re.search(
        r"\blist\s+(?:the\s+)?files\s+in\s+(?:my\s+|the\s+)?(.+)"
        r"|\bwhat\s+files\s+are\s+in\s+(?:my\s+|the\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    folder = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not folder:
        return None
    return {"folder": folder}


def _match_create_folder(text: str) -> dict | None:
    m = re.search(
        r"\b(?:create|make)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named)\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = m.group(1).strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_rename_file(text: str) -> dict | None:
    m = re.search(r"\brename\s+(.+?)\s+to\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    return {"source": m.group(1).strip(), "dest": m.group(2).strip().rstrip("?.")}


def _match_copy_file(text: str) -> dict | None:
    m = re.search(r"\bcopy\s+(.+?)\s+to\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    return {"source": m.group(1).strip(), "dest": m.group(2).strip().rstrip("?.")}


def _match_move_file(text: str) -> dict | None:
    m = re.search(r"\bmove\s+(.+?)\s+to\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    return {"source": m.group(1).strip(), "dest": m.group(2).strip().rstrip("?.")}


def _match_delete_file(text: str) -> dict | None:
    m = re.search(
        r"\bdelete\s+(?:the\s+)?file\s+(?:called|named)?\s*(.+)"
        r"|\bremove\s+(?:the\s+)?file\s+(?:called|named)?\s*(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_compress_archive(text: str) -> dict | None:
    m = re.search(
        r"\bcompress\s+(.+?)\s+into\s+(?:a\s+)?(?:zip|archive)\b"
        r"|\bcreate\s+an?\s+archive\s+of\s+(.+)"
        r"|\bzip\s+up\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_extract_archive(text: str) -> dict | None:
    m = re.search(
        r"\bextract\s+(.+)"
        r"|\bunzip\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_check_disk_space(text: str) -> dict | None:
    if re.search(
        r"\bhow\s+much\s+(?:disk\s+space|storage)\s+(?:do\s+i\s+have|is\s+left|is\s+free)\b"
        r"|\bcheck\s+(?:my\s+)?(?:disk\s+space|storage)\b"
        r"|\bdisk\s+space\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_empty_trash(text: str) -> dict | None:
    if re.search(r"\bempty\s+(?:the\s+)?(?:trash|recycle\s+bin)\b", text, re.IGNORECASE):
        return {}
    return None


def _match_open_recent_downloads(text: str) -> dict | None:
    if re.search(
        r"\bopen\s+(?:my\s+)?recent\s+downloads\b"
        r"|\bshow\s+me\s+what\s+i\s+downloaded\b"
        r"|\bopen\s+(?:my\s+)?downloads\b",
        text, re.IGNORECASE
    ):
        return {}
    return None


def _match_search_file_contents(text: str) -> dict | None:
    m = re.search(
        r"\bsearch\s+(?:my\s+)?files\s+for\s+(.+)"
        r"|\bfind\s+files\s+containing\s+(.+)"
        r"|\bsearch\s+(?:inside\s+)?(?:my\s+)?documents\s+for\s+(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    term = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not term:
        return None
    return {"term": term}


def _match_read_pdf(text: str) -> dict | None:
    # "called"/"named" required (not optional) in the first alternative —
    # otherwise it backtracks into treating "the"/"my" itself as the
    # filename for phrasing like "read the pdf called X" (reproduced live:
    # matched {"name": "the"}). Order matters too: this specific pattern
    # must be tried before the looser "read X pdf" one below, or the same
    # backtracking problem resurfaces.
    m = re.search(
        r"\bread\s+(?:the\s+|my\s+)?pdf\s+(?:called|named)\s+(.+)"
        r"|\bread\s+(?:the\s+|my\s+)?(.+?)\s+pdf\b",
        text, re.IGNORECASE
    )
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not name:
        return None
    return {"name": name}


def _match_open_file_manager_at(text: str) -> dict | None:
    m = re.search(
        r"\bopen\s+(?:the\s+)?file\s+manager\s+at\s+(?:my\s+)?(.+)"
        r"|\bbrowse\s+to\s+(?:my\s+)?(.+)",
        text, re.IGNORECASE
    )
    if not m:
        return None
    folder = next((g for g in m.groups() if g), "").strip().rstrip("?.")
    if not folder:
        return None
    return {"folder": folder}


def _match_list_desktop_folders(text: str) -> dict | None:
    if re.search(
        r"\b(?:list|show|get|display)\s+(?:all\s+(?:the\s+)?)?(?:folders|directories|files)\s+(?:(?:i\s+have\s+)?on\s+(?:my\s+)?desktop|in\s+(?:my\s+)?desktop)\b"
        r"|\b(?:list|show|what)\s+(?:desktop\s+(?:folders|files|directories)|(?:folders|files|directories)\s+(?:are\s+)?on\s+(?:my\s+)?desktop)\b"
        r"|\b(?:ls|dir)\s+desktop\b"
        r"|\bdesktop\s+(?:folders|files|directories)\b",
        text, re.IGNORECASE,
    ):
        return {}
    return None


SKILLS: list[tuple[str, Callable[[str], dict | None]]] = [
    # Multi-arg (source/dest) patterns first — distinctive "X to Y" shape.
    ("rename_file", _match_rename_file),
    ("copy_file", _match_copy_file),
    ("move_file", _match_move_file),
    # Specific-phrase skills before looser ones.
    ("search_file_contents", _match_search_file_contents),
    ("read_pdf", _match_read_pdf),
    ("find_file", _match_find_file),
    ("open_file_manager_at", _match_open_file_manager_at),
    ("open_recent_downloads", _match_open_recent_downloads),
    ("list_desktop_folders", _match_list_desktop_folders),
    ("list_files", _match_list_files),
    ("create_folder", _match_create_folder),
    ("delete_file", _match_delete_file),
    ("compress_archive", _match_compress_archive),
    ("extract_archive", _match_extract_archive),
    ("check_disk_space", _match_check_disk_space),
    ("empty_trash", _match_empty_trash),
    ("open_folder", _match_open_folder),
    ("open_file", _match_open_file),
]
