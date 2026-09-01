#!/usr/bin/env python3
"""B5 · Exit Criterion 2 — CLI IPC latency (phases.md B5).

Times the **raw Unix-socket round-trip** for a non-inference command, with `rich`
rendering and CLI process start-up excluded — just what the daemon owes a client:
accept -> read one JSONL line -> route (`health` -> bus request/report) -> write
one JSONL line.

100 sequential `health` requests, a fresh connection each (matching the real
`neuropaca` CLI, which connects per invocation). Also fires one `$ <canary>`
query so `scripts/validate_b5_privacy.py` has something that *should* have stayed
in RAM to hunt for on disk.

    uv run --extra spike python scripts/validate_b5_latency.py            # spawn own daemon
    uv run --extra spike python scripts/validate_b5_latency.py \\
        --socket $XDG_RUNTIME_DIR/neuropaca.sock

Exit 0 = max round-trip < 100 ms. Exit 1 = over budget.
Prints the data dir + canary for the privacy check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core import logging as np_logging
from neuropaca.core.config import Config
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_BUDGET_MS = 100.0
_N = 100


async def _round_trip(socket_path: str, payload: dict) -> tuple[dict, float]:
    t0 = time.perf_counter()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5)
    writer.close()
    await writer.wait_closed()
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return json.loads(line), dt_ms


async def _run(socket_path: str, canary: str) -> int:
    # warm the path once (first connect pays import/accept costs), then measure
    await _round_trip(socket_path, {"op": "health"})
    await _round_trip(socket_path, {"op": "query", "prefix": "$", "text": f"note: {canary}"})

    samples: list[float] = []
    for _ in range(_N):
        resp, dt_ms = await _round_trip(socket_path, {"op": "health"})
        if not resp.get("ok"):
            print(f"FAIL — health returned {resp}", file=sys.stderr)
            return 1
        samples.append(dt_ms)

    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[int(0.95 * _N) - 1]
    worst = samples[-1]
    print(f"health x{_N} raw socket round-trip (ms):")
    print(f"  min {samples[0]:.2f}  p50 {p50:.2f}  p95 {p95:.2f}  max {worst:.2f}")
    verdict = "PASS" if worst < _BUDGET_MS else "FAIL"
    print(f"\n=== RESULT ({verdict}) — max {worst:.2f} ms  (budget {_BUDGET_MS:.0f} ms) ===")
    return 0 if worst < _BUDGET_MS else 1


async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", help="hit a running daemon instead of spawning one")
    ap.add_argument("--data-dir", help="where the spawned daemon writes graph.json / log")
    args = ap.parse_args()

    canary = f"CANARY-{uuid.uuid4().hex}"

    if args.socket:
        rc = await _run(args.socket, canary)
        print(f"\ncanary (should be RAM-only): {canary}")
        return rc

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = Path(f"/tmp/neuropaca-b5-latency-{uuid.uuid4().hex[:8]}")
    data_dir.mkdir(parents=True, exist_ok=True)
    sock = str(data_dir / "neuropaca.sock")
    log_path = data_dir / "neuropaca.log"

    cfg = Config(
        inference_backend="fake",
        graph_db_path=str(data_dir / "graph.json"),
        action_log_path=str(data_dir / "actions.jsonl"),
        graph_save_interval_seconds=3600,
        interface_socket_path=sock,
        log_level="DEBUG",  # exercise the redacted IPC debug lines
    )
    orch = NeuroPACAOrchestrator(cfg, module_builder=build_modules)
    await orch.initialize()
    with log_path.open("w", encoding="utf-8") as logfile:
        np_logging.configure("DEBUG", stream=logfile)  # idempotent re-point to the file
        await orch.start()
        try:
            rc = await _run(sock, canary)
        finally:
            await orch.stop()

    print(f"\ndata dir : {data_dir}")
    print(f"canary   : {canary}")
    print(
        "next     : uv run python scripts/validate_b5_privacy.py "
        f"--data-dir {data_dir} --canary {canary}"
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
