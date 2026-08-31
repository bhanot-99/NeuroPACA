"""Stress · event-loop latency baseline (scripts/measure_loop_lag.py).

A short CI-enforceable smoke of the B4 baseline: with the real daemon running and
the watched directory under a file-write blast, a 10 ms probe task must never be
woken more than 50 ms late. If B1-B3 already stalls the loop, B4's in-process
``infer_async`` would freeze it — so this must stay green before B4 lands.

The full diagnostic (30 s, optional 10k-node graph) is the script itself:
``uv run --extra spike python scripts/measure_loop_lag.py``. Since T4 was fixed
(chunked ``recalculate_importance`` / ``save`` + ``gc.freeze``) the ``--big-graph``
run also passes; ``test_t4_loop_budget.py`` is the fast CI gate for that path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.measure_loop_lag import _infer_stress, _run  # noqa: E402

pytestmark = pytest.mark.stress


async def test_loop_lag_stays_under_50ms_during_a_file_blast() -> None:
    # _run returns the would-be process exit code: 0 iff max loop lag < 50 ms
    exit_code = await _run(seconds=4.0, big_graph=False)
    assert exit_code == 0, "event-loop lag exceeded the 50 ms B4 baseline (see printed report)"


async def test_loop_lag_stays_under_50ms_during_a_10s_inference() -> None:
    # B4 exit clause I — a ~10 s backend inference (real llama.cpp if the wheel +
    # a model are present, else a backend that blocks its thread ~2 s/call) must
    # not wake the 10 ms probe more than 50 ms late.
    exit_code = await _infer_stress()
    assert exit_code == 0, "inference stalled the event loop — executor offload broken (B4 exit I)"
