#!/usr/bin/env python3
"""B1 exit criteria · 1-hour RSS soak (phases.md B1 — "flat RSS over a 1 h soak").

Boots the daemon into its idle loop over the deterministic 10 000-node graph and
samples process RSS once a minute for an hour. The daemon does no real work while
idle — the EventBus queue is bounded, the graph is fixed, the singletons hold no
growing state — so RSS must stay flat. A rising trend means a leak.

    uv run --extra spike python scripts/soak_test_b1.py            # full hour
    uv run --extra spike python scripts/soak_test_b1.py --minutes 3   # smoke

Exit 0 if peak-to-trough RSS drift across minutes 5..end is < 5 %, else exit 1.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

try:
    import psutil
except ImportError:
    sys.exit("psutil is required — run: uv run --extra spike python scripts/soak_test_b1.py")

from tests.fixtures.generate_10k_graph import write_fixture

from neuropaca.core.config import Config
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_DRIFT_THRESHOLD = 0.05
_WARMUP_MINUTE = 5


async def _soak(minutes: int, interval_s: float) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-soak-"))
    graph_path = workdir / "graph_10k.json"
    write_fixture(graph_path)

    config = Config(
        inference_backend="fake",
        graph_db_path=str(graph_path),
        action_log_path=str(workdir / "actions.jsonl"),
        graph_save_interval_seconds=60,
        log_level="WARNING",
    )
    orchestrator = NeuroPACAOrchestrator(config)
    run_task = asyncio.create_task(orchestrator.run())
    await asyncio.sleep(1.0)  # let it boot and load the graph

    proc = psutil.Process()
    samples: list[tuple[int, float]] = []
    print(f"soak: {minutes} min, sampling every {interval_s:.0f}s\n")
    for minute in range(minutes + 1):
        rss_mib = proc.memory_info().rss / (1024 * 1024)
        samples.append((minute, rss_mib))
        print(f"  minute {minute:3d}   RSS {rss_mib:9.2f} MiB")
        if minute < minutes:
            await asyncio.sleep(interval_s)

    orchestrator.request_shutdown_nowait()
    await asyncio.wait_for(run_task, timeout=30)

    return _verdict(samples, minutes)


def _verdict(samples: list[tuple[int, float]], minutes: int) -> int:
    warmup = min(_WARMUP_MINUTE, minutes)
    window = [rss for minute, rss in samples if minute >= warmup]
    low, high = min(window), max(window)
    drift = (high - low) / low

    baseline = next(rss for minute, rss in samples if minute == warmup)
    endpoint = samples[-1][1]
    endpoint_drift = (endpoint - baseline) / baseline

    print(
        f"\nwindow [min {warmup}..{minutes}]  low {low:.2f}  high {high:.2f} MiB  "
        f"peak-to-trough drift {drift * 100:.2f}%"
    )
    print(
        f"min {warmup} -> {minutes}:  {baseline:.2f} -> {endpoint:.2f} MiB  "
        f"({endpoint_drift * 100:+.2f}%)"
    )

    if drift < _DRIFT_THRESHOLD:
        print(f"\nPASS — RSS drift {drift * 100:.2f}% < {_DRIFT_THRESHOLD * 100:.0f}%")
        return 0
    print(f"\nFAIL — RSS drift {drift * 100:.2f}% >= {_DRIFT_THRESHOLD * 100:.0f}% (possible leak)")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60, help="soak duration (default 60)")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between samples (default 60)"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_soak(args.minutes, args.interval)))


if __name__ == "__main__":
    main()
