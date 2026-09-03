#!/usr/bin/env python3
"""B7 · Exit Criteria 1 & 2 — the pressure gradient (phases.md B7, D-14).

Runs against the real box on the **real clock** — no `FakeClock`, no simulated
time — because the two criteria are statements about wall-clock behaviour:

  1. *a single signal never crosses the high threshold* — a burst of 500
     maximum-confidence L3 signals is fired at one node. Pressure reaches ~500x
     the low threshold and the high tier stays shut, because corroboration is a
     set test over {diagnosis, learning}, not a magnitude test.
  2. *pressure decays to < 1 % within 10 min of the last contribution* — with the
     half-life shortened to 6 s (a 1:10 time compression of the 60 s default, so
     "10 minutes" is 60 real seconds), the surviving fraction is measured against
     the exact exponential at ten half-lives.

Then, to prove the gradient does open when it should, L4 corroborates and the
high tier fires once — with both sources recorded on the entry.

    uv run python scripts/validate_b7_pressure.py

Exit 0 = all assertions pass. Exit 1 = any failure.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core import logging as np_logging
from neuropaca.core.clock import SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.drive.pressure import PressureAccumulator
from neuropaca.learning.insight import Insight

_BURST = 500
_HALF_LIFE_S = 6  # 1:10 compression — "10 minutes" of decay in 60 real seconds
_DECAY_WINDOW_S = 10 * _HALF_LIFE_S
_DECAY_BOUND = 0.01
_NODE = "app:webpack"


def _signal(node_id: str) -> Signal:
    return Signal(
        signal_type=SignalType.HIGH_LOAD,
        confidence=1.0,
        related_node_ids=(node_id,),
        source_snapshots=(),
        reason="cpu pinned at 100% for 90s",
    )


def _insight(node_id: str) -> Insight:
    return Insight(
        category="anomaly",
        cited_node_ids=(node_id,),
        source_signal=SignalType.HIGH_LOAD,
        confidence=1.0,
        snapshot_count=3,
        node_id="insight:validate",
    )


async def _main() -> int:
    np_logging.configure("WARNING")
    tmp = Path(tempfile.mkdtemp(prefix="np-b7-pressure-"))

    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp / "graph.json"))
    await graph.load()
    config = Config(
        inference_backend="fake",
        graph_db_path=str(tmp / "graph.json"),
        pressure_decay_half_life_seconds=_HALF_LIFE_S,
        pressure_decay_interval_seconds=2,
    )
    drive = PressureAccumulator(bus, config, graph, clock=SystemClock())
    await drive.initialize()
    await drive.start()

    crossings: list[tuple[str, float, tuple[str, ...]]] = []

    async def spy(event: Event) -> None:
        entry = event.payload["entry"]
        crossings.append((event.payload["tier"], entry.pressure, entry.sources))

    bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, spy)

    # --- 1. one source, 500 spikes -----------------------------------------
    t0 = time.perf_counter()
    for _ in range(_BURST):
        bus.publish(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": _signal(_NODE)},
            )
        )
    await bus.join()
    burst_elapsed = time.perf_counter() - t0
    peak = drive.current_pressure(_NODE)
    single_source_tiers = [tier for tier, _, _ in crossings]

    # --- 2. decay over ten half-lives --------------------------------------
    await asyncio.sleep(_DECAY_WINDOW_S)
    remaining = drive.current_pressure(_NODE)
    fraction = remaining / peak if peak else 1.0

    # --- 3. the gradient does open when both layers agree -------------------
    crossings.clear()
    for _ in range(4):
        bus.publish(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": _signal("app:corroborated")},
            )
        )
        bus.publish(
            Event(
                event_type=EventType.INSIGHT_GENERATED,
                source="learning",
                payload={"insight": _insight("app:corroborated")},
            )
        )
    await bus.join()
    corroborated_tiers = [tier for tier, _, _ in crossings]
    high = [c for c in crossings if c[0] == "high"]

    await drive.stop()
    await bus.stop()

    print(f"burst           : {_BURST} L3 signals in {burst_elapsed * 1000:.1f} ms")
    print(
        f"peak pressure   : {peak:.2f}  (low {config.pressure_low_threshold}, "
        f"high {config.pressure_high_threshold})"
    )
    print(f"tiers fired     : {single_source_tiers}")
    print(
        f"after {_DECAY_WINDOW_S}s     : {remaining:.6f}  ({fraction * 100:.4f} % of peak; "
        f"exact 0.5**10 = {0.5**10 * 100:.4f} %)"
    )
    print(f"corroborated    : {corroborated_tiers}")
    if high:
        print(f"high entry      : {high[0][1]:.2f} from {'+'.join(high[0][2])}")

    fails: list[str] = []
    if "high" in single_source_tiers:
        fails.append("a single source crossed the high threshold")
    if single_source_tiers != ["low"]:
        fails.append(f"expected exactly one low crossing, got {single_source_tiers}")
    if peak < config.pressure_high_threshold:
        fails.append(
            f"peak {peak:.2f} never exceeded the high threshold — the test is not "
            "exercising the corroboration gate"
        )
    if fraction >= _DECAY_BOUND:
        fails.append(f"decayed to {fraction * 100:.3f} % of peak, expected < 1 %")
    if "high" not in corroborated_tiers:
        fails.append("L3 + L4 corroboration did not open the high tier")
    if high and set(high[0][2]) != {"diagnosis", "learning"}:
        fails.append(f"high entry sources were {high[0][2]}, expected both layers")

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(
        f"=== RESULT (PASS) — {_BURST} single-source spikes ({peak:.1f}) never opened the "
        f"high tier; decayed to {fraction * 100:.4f} % in {_DECAY_WINDOW_S}s; "
        f"L3+L4 opened it once ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
