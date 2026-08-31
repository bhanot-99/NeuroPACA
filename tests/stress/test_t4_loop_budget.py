"""Stress · T4 regression gate — scheduler work must not stall the loop.

`recalculate_importance()` and `save()` on a 10k-node graph used to stall the
event loop ~320–370 ms per scheduler tick (problems.md T4). They are now chunked
(lock per bounded chunk, `await asyncio.sleep(0)` between) and the graph is
`gc.freeze()`d after load. This drives `scripts/benchmark_t4.py` and enforces the
< 50 ms budget so B4's in-process inference has a loop to run on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.benchmark_t4 import _run  # noqa: E402  (path bootstrap must precede)

pytestmark = pytest.mark.stress


async def test_recalculate_and_save_stay_under_the_50ms_loop_budget() -> None:
    # _run returns the would-be exit code: 0 iff max loop lag < 50 ms across
    # `rounds` recalculate+save cycles on the 10k fixture.
    exit_code = await _run(rounds=4)
    assert exit_code == 0, "recalculate_importance()/save() stalled the loop >= 50 ms (T4 regressed)"
