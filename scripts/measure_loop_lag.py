#!/usr/bin/env python3
"""Event-loop latency monitor for the B1-B3 daemon (Architecture.md §14, phases.md B4 prep).

Runs the real daemon (system + filesystem collectors + the graph scheduler) while:

- a **probe** task sleeps for 10 ms in a tight loop and records how many ms late
  the loop actually wakes it — this is event-loop lag
- a **blaster** writes files into the watched directory off-thread
  (``asyncio.to_thread``), so the only on-loop work is the daemon's own
  (watchdog callback marshalling, collector polls, L3 pattern eval, and — with
  ``--big-graph`` — the scheduler's ``save()`` / ``recalculate_importance()``
  over 10k nodes)

    uv run --extra spike python scripts/measure_loop_lag.py --seconds 30
    uv run --extra spike python scripts/measure_loop_lag.py --seconds 20 --big-graph

Exit 0 iff the **max** loop lag is strictly < 50 ms. That is the B4 baseline: if
B1-B3 already stalls the loop, B4's in-process ``infer_async`` will freeze it.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core.config import Config
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_PROBE_INTERVAL_S = 0.01
_LAG_LIMIT_MS = 50.0
_BLAST_BATCH = 100
_BLAST_DISTINCT_FILES = 200


async def _probe(samples: list[float], stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        expected = loop.time() + _PROBE_INTERVAL_S
        await asyncio.sleep(_PROBE_INTERVAL_S)
        samples.append((loop.time() - expected) * 1000.0)  # ms late


def _write_batch(watch_dir: Path, start_index: int) -> None:
    for k in range(_BLAST_BATCH):
        name = f"f{(start_index + k) % _BLAST_DISTINCT_FILES}.tmp"
        (watch_dir / name).write_text(str(start_index + k))


async def _blaster(watch_dir: Path, stop: asyncio.Event) -> int:
    written = 0
    while not stop.is_set():
        await asyncio.to_thread(_write_batch, watch_dir, written)
        written += _BLAST_BATCH
        await asyncio.sleep(0.01)  # ~10k file changes/sec, off-loop
    return written


async def _run(seconds: float, big_graph: bool) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-looplag-"))
    watch_dir = workdir / "watched"
    watch_dir.mkdir()
    graph_path = workdir / "graph.json"

    if big_graph:
        from tests.fixtures.generate_10k_graph import write_fixture

        write_fixture(graph_path)

    config = Config(
        inference_backend="fake",
        graph_db_path=str(graph_path),
        action_log_path=str(workdir / "actions.jsonl"),
        watch_paths=[str(watch_dir)],
        poll_intervals={"system": 1.0, "filesystem": 1.0},
        graph_save_interval_seconds=2,  # force real save() + recalculate during the run
        log_level="WARNING",
    )
    orchestrator = NeuroPACAOrchestrator(config, module_builder=build_modules)
    await orchestrator.initialize()
    await orchestrator.start()

    stop = asyncio.Event()
    samples: list[float] = []
    probe_task = asyncio.create_task(_probe(samples, stop))
    blast_task = asyncio.create_task(_blaster(watch_dir, stop))

    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        writes = await blast_task
        probe_task.cancel()
        try:
            await probe_task
        except asyncio.CancelledError:
            pass
        await orchestrator.stop()

    if len(samples) < 10:
        print(f"only {len(samples)} probe samples — run longer")
        return 1

    samples.sort()
    mean = statistics.fmean(samples)
    p99 = samples[min(len(samples) - 1, int(len(samples) * 0.99))]
    peak = samples[-1]

    print(f"loop lag over {len(samples)} probes ({writes} file writes, big_graph={big_graph}):")
    print(
        f"  mean {mean:6.2f} ms   p99 {p99:6.2f} ms   max {peak:6.2f} ms   "
        f"(limit {_LAG_LIMIT_MS:.0f} ms)"
    )
    if peak < _LAG_LIMIT_MS:
        print("PASS")
        return 0
    print("FAIL — the event loop stalled; B4 inference on this loop would freeze it")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=float, default=30.0, help="measurement window in seconds (default 30)"
    )
    parser.add_argument(
        "--big-graph",
        action="store_true",
        help="load a 10k-node graph so the scheduler's save()/recalculate stress the loop too",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.seconds, args.big_graph)))


if __name__ == "__main__":
    main()
