#!/usr/bin/env python3
"""B2.5 · Wayland file-descriptor leak soak (phases.md B2.5, D-9/D-10).

`WaylandIdleSource` and `WaylandWindowSource` each open a `wl_display` socket and
register it with `loop.add_reader(fd, ...)`. Nothing in the daemon lifecycle
re-opens them — one connection per source, closed in `stop()`. This script runs
the real `ActivityCollector` (both Wayland sources) for a few hours and samples
the process's **open file-descriptor count** and RSS. It FAILS if the fd count
grows with a sustained positive slope — the signature of a `Display.connect()` /
`add_reader` per event instead of once at start.

    # needs a COSMIC session + the activity extra
    uv run --extra activity python scripts/soak_test_b2_5.py                 # 2 h
    uv run --extra activity python scripts/soak_test_b2_5.py --hours 0.2     # smoke

Toggling the active window / idle state during the run is what exercises the
Wayland event path. Either:
  - **manual** — every few minutes: switch focus between 2-3 apps, then leave the
    keyboard/mouse for > `idle_threshold_seconds` and come back. Repeat.
  - **synthetic** — from another terminal, drive input with a tool that talks to
    the compositor, e.g. `while true; do wtype -k Tab; sleep 5; done` (needs
    `wtype`), or a `ydotool` key loop; stop it for 6+ min once an hour for a
    real idle gap.

Exit 0 = fd count flat (PASS). Exit 1 = fd growth detected (FAIL).
Exit 2 = could not run (no compositor / pywayland missing — the collector
self-disabled, so there is nothing to measure).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

try:
    import psutil
except ImportError:
    sys.exit("psutil is required — run with `uv run --extra spike ...` or install psutil")

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.sensing.activity.collector import ActivityCollector

# Fail if the fd-count trend over the post-warm-up window exceeds this slope.
# One extra fd per 10 min sustained over 2 h is ~12 leaked descriptors — clear of
# sampling jitter (+/- 1-2 fds) but a real per-event leak dwarfs it.
_FD_SLOPE_LIMIT_PER_MIN = 0.10
_WARMUP_SECONDS = 5 * 60
_RSS_DRIFT_LIMIT = 0.10


def _open_fds(proc: psutil.Process) -> int:
    try:
        return proc.num_fds()  # POSIX
    except AttributeError:  # pragma: no cover - Windows
        return len(proc.open_files()) + len(proc.net_connections(kind="all"))


def _slope_per_min(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y vs x (x in seconds), returned per minute."""
    n = len(points)
    if n < 2:
        return 0.0
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom * 60.0


async def _soak(hours: float, interval_s: float) -> int:
    bus = EventBus.get_instance()
    await bus.start()

    switches = 0
    transitions = 0

    async def _count_switch(_event: Event) -> None:
        nonlocal switches
        switches += 1

    async def _count_idle(_event: Event) -> None:
        nonlocal transitions
        transitions += 1

    bus.subscribe(EventType.APP_SWITCH, _count_switch)
    bus.subscribe(EventType.IDLE_DETECTED, _count_idle)
    bus.subscribe(EventType.ACTIVITY_DETECTED, _count_idle)

    config = Config(inference_backend="fake", activity_enabled=True, log_level="WARNING")
    collector = ActivityCollector(bus, config)
    await collector.initialize()
    await collector.start()
    await asyncio.sleep(2.0)

    detail = collector.health().detail
    if "idle✓" not in detail and "window✓" not in detail:
        print(f"activity collector started no Wayland source: {detail}")
        await collector.stop()
        await bus.stop()
        return 2
    print(f"activity collector: {detail}")

    proc = psutil.Process()
    fd_series: list[tuple[float, float]] = []  # (elapsed_s, open_fds)
    rss_series: list[float] = []

    total = round(hours * 3600 / interval_s)
    print(f"soak: {hours:g} h, sampling every {interval_s:.0f}s ({total} samples)")
    print("toggle window focus / idle state during the run — see the module docstring\n")

    for i in range(total + 1):
        elapsed = i * interval_s
        fds = _open_fds(proc)
        rss = proc.memory_info().rss / (1024 * 1024)
        fd_series.append((elapsed, float(fds)))
        rss_series.append(rss)
        print(
            f"  t+{elapsed / 60:6.1f} min   fds {fds:4d}   RSS {rss:8.2f} MiB   "
            f"switches {switches}   idle/active {transitions}"
        )
        if i < total:
            await asyncio.sleep(interval_s)

    await collector.stop()
    await bus.stop()
    return _verdict(fd_series, rss_series, switches, transitions)


def _verdict(
    fd_series: list[tuple[float, float]],
    rss_series: list[float],
    switches: int,
    transitions: int,
) -> int:
    steady = [(x, y) for x, y in fd_series if x >= _WARMUP_SECONDS]
    note = ""
    if len(steady) < 2:
        steady = fd_series
        note = " (run shorter than the 5-min warm-up — using all samples)"

    slope = _slope_per_min(steady)
    fd_low = min(y for _, y in steady)
    fd_high = max(y for _, y in steady)
    rss_steady = rss_series[len(rss_series) - len(steady) :] or rss_series
    rss_drift = (max(rss_steady) - min(rss_steady)) / min(rss_steady)

    print(
        f"\nfd count post-warm-up{note}: low {fd_low:.0f}  high {fd_high:.0f}  "
        f"slope {slope:+.3f} fd/min  (limit {_FD_SLOPE_LIMIT_PER_MIN:.2f})"
    )
    print(f"RSS drift: {rss_drift * 100:.2f}%  (limit {_RSS_DRIFT_LIMIT * 100:.0f}%)")
    print(f"observed {switches} APP_SWITCH · {transitions} idle/active transitions")

    if switches == 0 and transitions == 0:
        print(
            "\nFAIL — no Wayland events observed; nothing was exercised "
            "(toggle window focus / idle state during the run)"
        )
        return 1

    fd_ok = slope < _FD_SLOPE_LIMIT_PER_MIN
    rss_ok = rss_drift < _RSS_DRIFT_LIMIT
    if fd_ok and rss_ok:
        print("\nPASS — Wayland sockets are managed once, not per event")
        return 0
    reasons = []
    if not fd_ok:
        reasons.append(f"fd count grows {slope:+.3f}/min — likely a per-event connect/add_reader")
    if not rss_ok:
        reasons.append(f"RSS drift {rss_drift * 100:.1f}% exceeds {_RSS_DRIFT_LIMIT * 100:.0f}%")
    print("\nFAIL — " + "; ".join(reasons))
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=2.0, help="soak duration (default 2)")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between samples (default 60)"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_soak(args.hours, args.interval)))


if __name__ == "__main__":
    main()
