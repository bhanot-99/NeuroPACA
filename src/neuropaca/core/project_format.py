# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S2 · the "where you left off" formatter (VISION_PHASES.md), shared between
`sensing/project_ingest.py` (the live thread moment) and
`interface/briefing.py` (the morning-briefing candidate) — both render the
exact same extractive summary from the same fields, so the text lives in one
place under `core/` rather than two copies that could silently drift (the same
reasoning as `core/labels.py` for cross-layer rendering).
"""

from __future__ import annotations

_NUM_WORDS: dict[int, str] = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def format_project_left_off(
    name: str,
    branch: str,
    dirty_count: int,
    last_failing_tests: list[str],
    next_note: str | None,
) -> str:
    """Format the extractive "where you left off" summary.

    Only clauses that are true get included:
    - If dirty_count > 0: includes "with N uncommitted files"
    - If last_failing_tests: includes "the last failing test was <name>"
    - If next_note: includes "your note says: <note>"
    """
    clauses: list[str] = []
    base = f"You left {name} on {branch}"
    if dirty_count > 0:
        count_str = _NUM_WORDS.get(dirty_count, str(dirty_count))
        file_str = "file" if dirty_count == 1 else "files"
        base += f" with {count_str} uncommitted {file_str}"
    clauses.append(base)

    if last_failing_tests:
        last_test = last_failing_tests[-1]
        test_name = last_test.split("::")[-1]
        clauses.append(f"the last failing test was {test_name}")

    if next_note:
        clauses.append(f"your note says: {next_note}")

    return "; ".join(clauses) + "."


# gen-ref: 22346ab8
