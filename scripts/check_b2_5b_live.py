#!/usr/bin/env python3
"""B2.5b exit criterion · active-window live check on the target box (D-10).

Runs the real `ActivityCollector` (Wayland `zcosmic_toplevel_info_v1`) wired to a
real `SignalCorrelator` with the shipped `app_map.default.toml`, and prints every
`APP_SWITCH` with its classified domain plus any `FOCUS_SESSION` / `DISTRACTION`
signal. Needs a COSMIC session and the `activity` extra:

    uv run --extra activity python scripts/check_b2_5b_live.py            # runs until Ctrl-C
    uv run --extra activity python scripts/check_b2_5b_live.py --seconds 60

Exit 0 if at least one APP_SWITCH was read and classified; non-zero otherwise
(headless, wrong compositor, or pywayland missing — the collector self-disables).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.sensing.activity.collector import ActivityCollector


async def _run(seconds: float | None) -> int:
    switches = 0
    classified = 0
    signals = 0

    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(_ROOT / "data" / "graph.json"))
    await graph.load()
    config = Config(inference_backend="fake", activity_enabled=True)
    correlator = SignalCorrelator(bus, config, graph)
    activity = ActivityCollector(bus, config)

    async def _on_switch(event: Event) -> None:
        nonlocal switches, classified
        switches += 1
        app_id = event.payload.get("app_id")
        domain = correlator._app_map.classify(app_id) or "—"
        if domain != "—":
            classified += 1
        print(f"  APP_SWITCH  {app_id!r:40}  ->  {domain}")

    async def _on_signal(event: Event) -> None:
        nonlocal signals
        signals += 1
        sig = event.payload["signal"]
        print(f"  SIGNAL      {sig.signal_type:14}  conf={sig.confidence:.2f}  {sig.reason}")

    bus.subscribe(EventType.APP_SWITCH, _on_switch)
    bus.subscribe(EventType.SIGNAL_CORRELATED, _on_signal)

    for module in (correlator, activity):
        await module.initialize()
        await module.start()

    print(f"app_map rules loaded: {correlator._app_map.rule_count}")
    print(f"activity collector: {activity.health().detail}")
    print("switch focus between apps now (Ctrl-C to stop)…\n")

    try:
        await (asyncio.sleep(seconds) if seconds else asyncio.Event().wait())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for module in (activity, correlator):
            await module.stop()
        await bus.stop()

    print(f"\n{switches} APP_SWITCH · {classified} classified · {signals} signals")
    return 0 if switches and classified else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    args = ap.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args.seconds)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
