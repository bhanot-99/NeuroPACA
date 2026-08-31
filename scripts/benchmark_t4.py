#!/usr/bin/env python3
"""T4 before/after harness — does the scheduler stall the event loop? (problems.md T4).

Loads the 10k-node graph fixture, starts a background loop-lag probe (10 ms sleep,
measures how late it wakes), then triggers ``recalculate_importance()`` and
``save()`` sequentially a few times — exactly what the scheduler ``_tick`` does.

    uv run --extra spike python scripts/benchmark_t4.py
    uv run --extra spike python scripts/benchmark_t4.py --rounds 5

Exit 0 iff the **max** observed loop lag is strictly < 50 ms. This is the pass/fail
gate for the T4 fixes (chunked recalculate + fully thread-offloaded save).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core.graph_memory import GraphMemory

_PROBE_INTERVAL_S = 0.01
_LAG_LIMIT_MS = 50.0


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + _PROBE_INTERVAL_S
        await asyncio.sleep(_PROBE_INTERVAL_S)
        samples.append((loop.time() - expected) * 1000.0)


async def _run(rounds: int) -> int:
    from tests.fixtures.generate_10k_graph import FIXTURE_PATH, write_fixture

    if not FIXTURE_PATH.exists():
        write_fixture(FIXTURE_PATH)

    graph = GraphMemory.get_instance(persistence_path=str(FIXTURE_PATH))
    await graph.load()
    print(f"loaded {graph.node_count} nodes / {graph.edge_count} edges")

    save_path = _ROOT / "scratchpad" / "t4_bench_graph.json"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    graph._path = save_path  # write the benchmark copy, not the fixture

    stop = asyncio.Event()
    samples: list[float] = []
    probe = asyncio.create_task(_probe(samples, stop))
    await asyncio.sleep(0.2)  # let the probe settle

    per_phase: list[tuple[str, float, float]] = []  # (label, wall_ms, max_lag_during_ms)
    for r in range(rounds):
        for phase, run_phase in (
            ("recalculate_importance", graph.recalculate_importance),
            ("save", graph.save),
        ):
            mark = len(samples)
            t0 = time.perf_counter()
            await run_phase()
            wall_ms = (time.perf_counter() - t0) * 1000.0
            await asyncio.sleep(0.05)  # let this phase's probe samples land
            during = samples[mark:] or [0.0]
            per_phase.append((f"round {r} · {phase}", wall_ms, max(during)))

    stop.set()
    probe.cancel()
    try:
        await probe
    except asyncio.CancelledError:
        pass

    print("\nphase                        wall_ms   max_loop_lag_ms")
    for label, wall_ms, lag in per_phase:
        print(f"  {label:<25} {wall_ms:8.1f}   {lag:8.1f}")

    peak = max((lag for _, _, lag in per_phase), default=0.0)
    mean = statistics.fmean(samples) if samples else 0.0
    print(
        f"\nprobes {len(samples)}   mean lag {mean:.2f} ms   "
        f"MAX loop lag {peak:.1f} ms   (limit {_LAG_LIMIT_MS:.0f} ms)"
    )

    save_path.unlink(missing_ok=True)
    for leftover in save_path.parent.glob(save_path.name + ".*"):
        leftover.unlink(missing_ok=True)

    if peak < _LAG_LIMIT_MS:
        print("PASS")
        return 0
    print("FAIL — heavy graph work is stalling the event loop (T4)")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3, help="recalculate+save cycles (default 3)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.rounds)))


if __name__ == "__main__":
    main()
