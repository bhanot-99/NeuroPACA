#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Deterministic authorship-provenance stamper for NeuroPACA.

Idempotently inserts an SPDX license header at the top of every first-party
Python source file and appends a per-file ``gen-ref`` marker derived from a
private secret. Safe to re-run after any merge: files already stamped are left
untouched, so new files picked up from a merged branch get stamped and nothing
else changes.

Usage::

    PROV_SECRET='<secret phrase>' python scripts/_provenance.py

The secret is read only from the ``PROV_SECRET`` environment variable; it is
never written to a file, a log, or an argv entry. The marker for one file is::

    sha256(f"{PROV_SECRET}:{posix_relative_path}").hexdigest()[:8]

where ``posix_relative_path`` is the file's path relative to the repository
root. To prove authorship later, reveal the secret and this recipe, re-derive
the marker for every file, and show it matches.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# Directories walked for *.py sources.
SCAN_ROOTS = ("src", "tests", "scripts")

# Any path segment in this set excludes the file (generated bindings, caches,
# build trees, virtualenvs, VCS metadata).
EXCLUDE_SEGMENTS = {".git", ".venv", "__pycache__", "build", "dist", "_protocols"}

SPDX_MARK = "SPDX-License-Identifier"
REF_MARK = "# gen-ref:"
HEADER = (
    "# SPDX-License-Identifier: AGPL-3.0-only\n"
    "# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>\n"
)


def _excluded(rel: Path) -> bool:
    if (ROOT / rel).resolve() == SELF:
        return True
    if set(rel.parts) & EXCLUDE_SEGMENTS:
        return True
    return any(part.endswith(".egg-info") for part in rel.parts)


def _iter_files() -> list[Path]:
    found: list[Path] = []
    for name in SCAN_ROOTS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if not _excluded(path.relative_to(ROOT)):
                found.append(path)
    return found


def gen_ref(rel_posix: str, secret: str) -> str:
    return hashlib.sha256(f"{secret}:{rel_posix}".encode()).hexdigest()[:8]


def _apply_header(text: str) -> tuple[str, bool]:
    if SPDX_MARK in text:
        return text, False
    lines = text.split("\n")
    if lines and lines[0].startswith("#!"):
        shebang = lines[0] + "\n"
        rest = "\n".join(lines[1:])
    else:
        shebang = ""
        rest = text
    rest = rest.lstrip("\n")
    return f"{shebang}{HEADER}\n{rest}", True


def _apply_ref(text: str, ref: str) -> tuple[str, bool]:
    if REF_MARK in text:
        return text, False
    # two blank lines: ruff format requires them before a module-level comment
    # that follows a top-level def/class, and CI runs `ruff format --check`
    return f"{text.rstrip()}\n\n\n{REF_MARK} {ref}\n", True


def main() -> int:
    secret = os.environ.get("PROV_SECRET")
    if not secret:
        print(
            "error: set PROV_SECRET in the environment (never as an argument)",
            file=sys.stderr,
        )
        return 2

    files = _iter_files()
    headers_added = 0
    refs_added = 0
    for path in files:
        rel_posix = path.relative_to(ROOT).as_posix()
        original = path.read_text(encoding="utf-8")
        text, added_header = _apply_header(original)
        text, added_ref = _apply_ref(text, gen_ref(rel_posix, secret))
        if text != original:
            path.write_text(text, encoding="utf-8")
        headers_added += int(added_header)
        refs_added += int(added_ref)

    print(f"scanned {len(files)} file(s); headers added: {headers_added}; refs added: {refs_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
