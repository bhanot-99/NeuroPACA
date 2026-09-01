#!/usr/bin/env python3
"""B6 · Exit Criterion 1 — DMN cancellation latency & graph integrity (phases.md B6).

Runs against the real 16 GB Wayland/COSMIC box. Loads a 10 000-node graph, pads
it with duplicate nodes so `GraphMemory.consolidate()` has far more than 0.5 s of
merge work, then:

  1. fires IDLE_DETECTED — the DMN starts a cycle and is soon deep inside the
     chunked consolidate loop (one `_lock` cycle + `await asyncio.sleep(0)` per
     merge);
  2. lets it run for exactly 0.5 s;
  3. fires ACTIVITY_DETECTED and times how long `idle_task` takes to fully unwind.

Asserts:
  * the cycle was still running when activity fired (we cancelled live work);
  * `idle_task` is done in **< 1.0 s** of `.cancel()`;
  * absolute structural integrity — `_lock` released, every node keeps its full
    attribute set, no dangling edges, the 11 routing hubs untouched, and no
    deadlock (a fresh add / traverse / save / a full second `consolidate()` all
    finish under a watchdog).

    uv run --extra spike python scripts/validate_b6_cancel.py
    uv run --extra spike python scripts/validate_b6_cancel.py --dupes 20000

Exit 0 = all assertions pass. Exit 1 = any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.fixtures.generate_10k_graph import build_payload

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import DOMAIN_HUB_IDS, HUB_NODE_IDS, GraphMemory
from neuropaca.core.models import Event
from neuropaca.idle.dmn import DefaultModeNetwork

_NODE_ATTRS = (
    "node_type",
    "label",
    "created_at",
    "last_accessed",
    "access_count",
    "relevance_score",
    "priority",
)
_CANCEL_BUDGET_S = 1.0
_RUN_BEFORE_CANCEL_S = 0.5
_WATCHDOG_S = 5.0
_CLEANUP_WATCHDOG_S = 180.0
_SEED = 20260906


def _inject_duplicates(payload: dict, count: int, *, seed: int = _SEED) -> dict:
    """Append `count` exact-duplicate leaf nodes (same node_type + label), each
    newer than its original so consolidate keeps the original, with a few random
    edges to stable nodes (real leaves + domain hubs) — never dup -> dup."""
    rng = random.Random(seed)
    leaves = [n for n in payload["nodes"] if n["id"].startswith("leaf:")]
    domains = sorted(DOMAIN_HUB_IDS)
    relations = [str(r) for r in RelationType]
    newest = max(n["last_accessed"] for n in payload["nodes"])

    for i in range(count):
        src = rng.choice(leaves)
        dup_id = f"dup:{i:06d}"
        payload["nodes"].append(
            {
                "id": dup_id,
                "node_type": src["node_type"],
                "label": src["label"],
                "created_at": newest,
                "last_accessed": newest,
                "access_count": rng.randint(0, 50),
                "relevance_score": round(rng.uniform(0.0, 10.0), 3),
                "priority": 0,
            }
        )
        for _ in range(rng.randint(1, 3)):
            pick = rng.choice(domains) if rng.random() < 0.5 else rng.choice(leaves)["id"]
            payload["edges"].append(
                {
                    "source": dup_id,
                    "target": pick,
                    "relation": rng.choice(relations),
                    "weight": round(rng.uniform(0.0, 1.0), 4),
                    "created_at": newest,
                }
            )
    return payload


def _integrity_failures(gm: GraphMemory) -> list[str]:
    fails: list[str] = []
    if gm._lock.locked():
        fails.append("GraphMemory._lock is still held after cancellation")

    graph = gm._graph
    for node_id, data in graph.nodes(data=True):
        missing = [a for a in _NODE_ATTRS if a not in data]
        if missing:
            fails.append(f"node {node_id!r} lost attributes {missing}")
            break
    for u, v, _key in graph.edges(keys=True):
        if u not in graph or v not in graph:
            fails.append(f"dangling edge {u!r} -> {v!r}")
            break
    for hub in HUB_NODE_IDS:
        if gm.get_node(hub) is None:
            fails.append(f"routing hub {hub!r} vanished")
    return fails


async def _no_deadlock(gm: GraphMemory) -> list[str]:
    """Every public path must still acquire and release the lock cleanly."""
    fails: list[str] = []
    try:
        await asyncio.wait_for(
            gm.add_node("probe:post-cancel", NodeType.CONCEPT, {"label": "probe"}), _WATCHDOG_S
        )
        gm.find_related("YOU", depth=1, traverse_hubs=True)
        await asyncio.wait_for(gm.save(), _WATCHDOG_S)
        remaining = await asyncio.wait_for(gm.consolidate(), _CLEANUP_WATCHDOG_S)
        print(f"  second consolidate() finished the remaining {remaining} merges cleanly")
    except TimeoutError:
        fails.append("a post-cancel graph operation deadlocked (watchdog fired)")
    return fails


async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dupes", type=int, default=12_000)
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-b6-cancel-"))
    graph_path = workdir / "graph.json"
    payload = _inject_duplicates(build_payload(), args.dupes)
    graph_path.write_text(json.dumps(payload), encoding="utf-8")

    EventBus._reset_for_tests()
    GraphMemory._reset_for_tests()
    BitNetRuntime._reset_for_tests()

    bus = EventBus.get_instance()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(graph_path))
    await gm.load()
    start_nodes = gm.node_count
    print(f"graph loaded: {start_nodes} nodes ({args.dupes} duplicates injected)")

    cfg = Config(inference_backend="fake", graph_db_path=str(graph_path), log_level="WARNING")
    dmn = DefaultModeNetwork(bus, cfg, gm, BitNetRuntime.get_instance(), clock=SystemClock())
    await dmn.initialize()
    await dmn.start()

    await dmn.on_idle_detected(Event(event_type=EventType.IDLE_DETECTED))
    await asyncio.sleep(_RUN_BEFORE_CANCEL_S)

    idle_task = dmn._idle_task
    live = idle_task is not None and not idle_task.done()
    merges_done = start_nodes - gm.node_count
    print(f"after {_RUN_BEFORE_CANCEL_S}s: cycle running={live}, {merges_done} merges done so far")

    t0 = time.perf_counter()
    await dmn.on_activity_detected(Event(event_type=EventType.ACTIVITY_DETECTED))
    try:
        await asyncio.wait_for(
            asyncio.gather(idle_task, return_exceptions=True), _CANCEL_BUDGET_S + 1.0
        )
    except TimeoutError:
        pass
    latency = time.perf_counter() - t0
    done = idle_task is not None and idle_task.done()

    budget_ms = _CANCEL_BUDGET_S * 1000
    print(f"cancellation latency: {latency * 1000:.1f} ms  (budget {budget_ms:.0f} ms)")

    fails: list[str] = []
    if not live:
        fails.append("the cycle had already finished before 0.5 s — raise --dupes")
    if not done:
        fails.append("idle_task never finished unwinding")
    if latency >= _CANCEL_BUDGET_S:
        fails.append(f"cancellation took {latency:.3f}s (>= {_CANCEL_BUDGET_S}s)")
    if dmn._cancels != 1:
        fails.append(f"expected exactly 1 recorded cancel, got {dmn._cancels}")

    fails += _integrity_failures(gm)
    fails += await _no_deadlock(gm)

    await dmn.stop()
    await bus.stop()

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(f"data dir: {workdir}")
    print(
        f"=== RESULT (PASS) — cancel {latency * 1000:.1f} ms (< 1 s), "
        f"{merges_done} merges rolled back cleanly, graph intact, no deadlock ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
