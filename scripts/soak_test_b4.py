#!/usr/bin/env python3
"""B4 · full-pipeline memory gate (phases.md B4, D-11).

Replays the recorded B3 + B2.5 traces (`tests/fixtures/trace_*.json`) into a live
`EventBus` -> `SignalCorrelator` (L3) -> `BitNetPlasticity` (L4) for `--hours`,
sampling process RSS with `psutil`. The point is the L4 memory profile:

- **with the `[llama]` extra + a GGUF present** — the model lazy-loads on the
  first correlated signal; RSS must jump to and **plateau** around the documented
  ~1.39 GB (PRD §9) with no linear drift over the run.
- **without it** — `LlamaCppBackend` self-disables; RSS must hold at the base
  daemon footprint (graph + L3 deques), well under 300 MB.

    uv run --extra spike python scripts/soak_test_b4.py                  # 1 h, fake backend
    uv run --extra llama --extra spike python scripts/soak_test_b4.py    # 1 h, real model
    uv run --extra spike python scripts/soak_test_b4.py --hours 0.1      # smoke

Exit 0 = within the expected band + no drift. Exit 1 = drift / out of band.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

try:
    import psutil
except ImportError:
    sys.exit("psutil is required — run with `uv run --extra spike ...`")

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import create_backend
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.sensing.snapshot import MetricSnapshot

_FIXTURES = _ROOT / "tests" / "fixtures"
_TRACES = (
    "trace_highload.json",
    "trace_idle.json",
    "trace_noise.json",
    "trace_focus.json",
    "trace_distraction.json",
    "trace_calm.json",
)
_WARMUP_SECONDS = 5 * 60
_LLAMA_BAND_MB = (1_150.0, 1_800.0)  # ~1.39 GB documented, generous either side
_LLAMA_DRIFT_LIMIT = 0.08  # KV-cache churn is real; a leak is not
_BASE_FOOTPRINT_MB = 300.0
_BASE_DRIFT_LIMIT = 0.05
_APP_MAP = _ROOT / "data" / "app_map.default.toml"


def _load_events() -> list[Event]:
    """Flatten every trace into `METRIC_COLLECTED` / `APP_SWITCH` events. The
    original timestamps are irrelevant here — the daemon only cares about order
    and relative spacing, which the replay loop re-imposes."""
    events: list[Event] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for name in _TRACES:
        path = _FIXTURES / name
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "snapshots" in raw:  # B3-style
            for i, item in enumerate(raw["snapshots"]):
                snap = MetricSnapshot(
                    collector_name=str(item["collector_name"]),
                    timestamp=base + timedelta(seconds=i * 60),
                    data=dict(item["data"]),
                )
                events.append(
                    Event(
                        event_type=EventType.METRIC_COLLECTED,
                        source=f"sensing.{snap.collector_name}",
                        payload={"snapshot": snap},
                    )
                )
        else:  # B2.5-style mixed events
            for i, item in enumerate(sorted(raw["events"], key=lambda e: e["offset"])):
                ts = base + timedelta(seconds=i * 30)
                if item["kind"] == "metric":
                    snap = MetricSnapshot(
                        collector_name=str(item["collector_name"]),
                        timestamp=ts,
                        data=dict(item["data"]),
                    )
                    events.append(
                        Event(
                            event_type=EventType.METRIC_COLLECTED,
                            source=f"sensing.{snap.collector_name}",
                            payload={"snapshot": snap},
                        )
                    )
                else:
                    events.append(
                        Event(
                            event_type=EventType.APP_SWITCH,
                            source="sensing.activity",
                            payload={
                                "app_id": item["app_id"],
                                "title": item.get("title", ""),
                                "previous_app_id": None,
                            },
                            timestamp=ts,
                        )
                    )
    return events


def _slope_per_min(points: list[tuple[float, float]]) -> float:
    n = len(points)
    if n < 2:
        return 0.0
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    return 0.0 if denom == 0 else (n * sxy - sx * sy) / denom * 60.0


async def _soak(hours: float, interval_s: float) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-soak-b4-"))

    ggufs = sorted((_ROOT / "models").glob("*.gguf")) if (_ROOT / "models").is_dir() else []
    try:
        import llama_cpp  # noqa: F401

        have_llama = True
    except ImportError:
        have_llama = False
    use_real = have_llama and bool(ggufs)
    config = Config(
        inference_backend="llama" if use_real else "fake",
        model_path=str(ggufs[0]) if use_real else "",
        graph_db_path=str(workdir / "graph.json"),
        app_map_path=str(_APP_MAP),
        log_level="WARNING",
    )
    print(f"backend: {config.inference_backend}  (llama_cpp={have_llama}, gguf={len(ggufs)})")

    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=config.graph_db_path)
    await graph.load()
    runtime = BitNetRuntime.get_instance(create_backend(config))
    correlator = SignalCorrelator(bus, config, graph)
    learning = BitNetPlasticity(bus, config, graph, runtime)
    for mod in (correlator, learning):
        await mod.initialize()
        await mod.start()

    events = _load_events()
    if not events:
        print("no trace fixtures found — run `python -m tests.fixtures.generate_b2_5_traces` first")
        return 1
    print(f"{len(events)} events per replay cycle")

    proc = psutil.Process()
    samples: list[tuple[float, float]] = []  # (elapsed_s, rss_mb)
    next_sample = 0.0
    start = datetime.now(UTC)
    cycle = 0

    while (elapsed := (datetime.now(UTC) - start).total_seconds()) < hours * 3600:
        for event in events:
            if event.event_type is EventType.METRIC_COLLECTED:
                await correlator.on_metric_event(event)
            else:
                await correlator.on_app_switch(event)
            await bus.join()
        cycle += 1
        if elapsed >= next_sample:
            rss = proc.memory_info().rss / (1024 * 1024)
            samples.append((elapsed, rss))
            print(
                f"  t+{elapsed / 60:6.1f} min   RSS {rss:9.2f} MiB   "
                f"cycles {cycle}   insights {learning._generated}   "
                f"model {'loaded' if runtime.is_loaded else 'lazy'}"
            )
            next_sample = elapsed + interval_s
        await asyncio.sleep(0.05)

    for mod in (learning, correlator):
        await mod.stop()
    await bus.stop()
    return _verdict(samples, runtime.is_loaded)


def _verdict(samples: list[tuple[float, float]], model_loaded: bool) -> int:
    if len(samples) < 2:
        print("too few samples")
        return 1
    steady = [(x, y) for x, y in samples if x >= _WARMUP_SECONDS] or samples
    lo = min(y for _, y in steady)
    hi = max(y for _, y in steady)
    slope = _slope_per_min(steady)
    drift = (hi - lo) / lo

    band = _LLAMA_BAND_MB if model_loaded else (0.0, _BASE_FOOTPRINT_MB)
    drift_limit = _LLAMA_DRIFT_LIMIT if model_loaded else _BASE_DRIFT_LIMIT
    peak = max(y for _, y in samples)

    print(
        f"\nmodel {'LOADED' if model_loaded else 'not loaded (fake / no wheel)'}\n"
        f"steady RSS [{_WARMUP_SECONDS // 60} min..end]: low {lo:.0f}  high {hi:.0f}  "
        f"peak {peak:.0f} MiB  drift {drift * 100:.2f}%  slope {slope:+.3f} MiB/min"
    )
    print(f"expected band {band[0]:.0f}-{band[1]:.0f} MiB, drift < {drift_limit * 100:.0f}%")

    ok_band = band[0] <= lo and peak <= band[1]
    ok_drift = drift < drift_limit and slope < 1.0
    if ok_band and ok_drift:
        print("\nPASS — RSS plateaus in band with no linear drift")
        return 0
    reasons = []
    if not ok_band:
        reasons.append(f"RSS out of the {band[0]:.0f}-{band[1]:.0f} MiB band")
    if not ok_drift:
        reasons.append(f"drift {drift * 100:.1f}% / slope {slope:+.2f} MiB/min looks like a leak")
    print("\nFAIL — " + "; ".join(reasons))
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=1.0, help="soak duration (default 1)")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between RSS samples")
    args = ap.parse_args()
    sys.exit(asyncio.run(_soak(args.hours, args.interval)))


if __name__ == "__main__":
    main()
