#!/usr/bin/env python3
"""B2 exit criteria · 24-hour telemetry soak (phases.md B2).

Boots the full daemon with the `system` and `filesystem` collectors both
polling, then samples the process's CPU% and RSS once a minute.

    uv run --extra spike python scripts/soak_test_b2.py              # 24 h
    uv run --extra spike python scripts/soak_test_b2.py --hours 0.5  # dry run

Pass (exit 0) requires BOTH:
  - mean CPU usage over the whole run  < 1 %
  - RSS peak-to-trough drift from minute 30 to the end < 5 %
    (the first 30 minutes are ignored — glibc/pymalloc arena warm-up, problems.md T2)
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
    sys.exit("psutil is required — run: uv run --extra spike python scripts/soak_test_b2.py")

from neuropaca.core.config import Config
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_CPU_MEAN_LIMIT = 1.0
_RSS_DRIFT_LIMIT = 0.05
_WARMUP_SECONDS = 30 * 60


async def _soak(hours: float, interval_s: float) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-soak-b2-"))
    watch_dir = workdir / "watched"
    watch_dir.mkdir()

    config = Config(
        inference_backend="fake",
        graph_db_path=str(workdir / "graph.json"),
        action_log_path=str(workdir / "actions.jsonl"),
        watch_paths=[str(watch_dir)],
        poll_intervals={"system": 60.0, "filesystem": 60.0},
        graph_save_interval_seconds=300,
        log_level="WARNING",
    )
    orchestrator = NeuroPACAOrchestrator(config, module_builder=build_modules)
    run_task = asyncio.create_task(orchestrator.run())
    await asyncio.sleep(2.0)  # boot + first collector prime

    proc = psutil.Process()
    proc.cpu_percent(interval=None)  # prime — the next call measures the window since now

    samples: list[tuple[float, float, float]] = []  # (elapsed_s, cpu_pct, rss_mib)
    total = round(hours * 3600 / interval_s)
    print(f"soak: {hours:g} h, sampling every {interval_s:.0f}s ({total} samples)\n")

    for i in range(total + 1):
        elapsed = i * interval_s
        cpu = proc.cpu_percent(interval=None)
        rss = proc.memory_info().rss / (1024 * 1024)
        samples.append((elapsed, cpu, rss))
        (watch_dir / f"tick_{i}.tmp").write_text("x")  # keep the filesystem path warm
        print(f"  t+{elapsed / 60:6.1f} min   CPU {cpu:5.2f}%   RSS {rss:9.2f} MiB")
        if i < total:
            await asyncio.sleep(interval_s)

    orchestrator.request_shutdown_nowait()
    await asyncio.wait_for(run_task, timeout=60)

    return _verdict(samples)


def _verdict(samples: list[tuple[float, float, float]]) -> int:
    # Drop sample 0 — cpu_percent there covers only the ~2 s prime window.
    cpus = [cpu for _, cpu, _ in samples[1:]] or [cpu for _, cpu, _ in samples]
    mean_cpu = sum(cpus) / len(cpus)

    steady = [rss for elapsed, _, rss in samples if elapsed >= _WARMUP_SECONDS]
    warmup_note = ""
    if len(steady) < 2:
        steady = [rss for _, _, rss in samples]
        warmup_note = " (run shorter than the 30-min warm-up window — using all samples)"
    low, high = min(steady), max(steady)
    rss_drift = (high - low) / low

    print(f"\nmean CPU over {len(cpus)} samples: {mean_cpu:.3f}%  (limit {_CPU_MEAN_LIMIT}%)")
    print(
        f"RSS post-warm-up [{_WARMUP_SECONDS // 60} min..end]{warmup_note}: "
        f"low {low:.2f}  high {high:.2f} MiB  drift {rss_drift * 100:.2f}%  "
        f"(limit {_RSS_DRIFT_LIMIT * 100:.0f}%)"
    )

    cpu_ok = mean_cpu < _CPU_MEAN_LIMIT
    rss_ok = rss_drift < _RSS_DRIFT_LIMIT
    if cpu_ok and rss_ok:
        print("\nPASS")
        return 0
    print(
        f"\nFAIL — {'CPU mean too high; ' if not cpu_ok else ''}"
        f"{'RSS drift exceeds limit' if not rss_ok else ''}".strip()
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0, help="soak duration (default 24)")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between samples (default 60)"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_soak(args.hours, args.interval)))


if __name__ == "__main__":
    main()
