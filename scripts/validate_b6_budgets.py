#!/usr/bin/env python3
"""B6 · Exit Criterion 2 — DMN cycle budgets (phases.md B6).

Runs against the real 16 GB Wayland/COSMIC box. Boots the full daemon
(`build_modules`), but seeds `BitNetRuntime` with a backend whose every `infer()`
blocks for 4 s in the inference executor. With
`dmn_cycle_wall_clock_seconds = 10` and `dmn_max_inferences_per_cycle = 3`:

  * inference #1 finishes at ~4 s  -> 1 idle thought
  * inference #2 finishes at ~8 s  -> 2 idle thoughts
  * inference #3 starts at ~8 s and is still blocked when `asyncio.timeout(10)`
    fires -> the cycle aborts via `TimeoutError`

Asserts:
  * the cycle aborts on the wall-clock boundary (elapsed in [9.0, 11.5] s);
  * exactly **2** idle-thought nodes were produced (capped below the budget of 3);
  * `TimeoutError` was handled — `_timeouts == 1`, `_errors == 0`, no exception
    escaped, and the daemon is still running and healthy afterwards.

    uv run --extra spike python scripts/validate_b6_budgets.py

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
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_BLOCK_S = 4.0
_WALL_CLOCK_S = 10
_INFER_BUDGET = 3
_EXPECTED_THOUGHTS = 2
_ELAPSED_LOW, _ELAPSED_HIGH = 9.0, 11.5

_VALID_PROACTIVE = '{"subject": "n1", "object": "n2", "query_template": "how_does_x_affect_y"}'


class _BlockingBackend:
    """`InferenceBackend` whose `infer()` blocks `block_s` in the executor thread
    (never on the loop) and then returns a schema-valid proactive selection."""

    def __init__(self, block_s: float) -> None:
        self._block_s = block_s
        self.calls = 0

    @property
    def is_loaded(self) -> bool:
        return True

    def load(self) -> None:
        return None

    def unload(self) -> None:
        return None

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        self.calls += 1
        time.sleep(self._block_s)
        return _VALID_PROACTIVE

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return 0.0


async def _main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-b6-budgets-"))
    graph_path = workdir / "graph.json"
    log_path = workdir / "neuropaca.log"

    EventBus._reset_for_tests()
    GraphMemory._reset_for_tests()
    BitNetRuntime._reset_for_tests()

    backend = _BlockingBackend(_BLOCK_S)
    BitNetRuntime.get_instance(backend, backend)  # seed the singleton before the orchestrator

    cfg = Config(
        inference_backend="fake",  # ignored — the singleton is already seeded
        graph_db_path=str(graph_path),
        action_log_path=str(workdir / "actions.jsonl"),
        graph_save_interval_seconds=3600,
        dmn_cycle_wall_clock_seconds=_WALL_CLOCK_S,
        dmn_max_inferences_per_cycle=_INFER_BUDGET,
        dmn_top_k=5,
        log_level="WARNING",
    )

    orch = NeuroPACAOrchestrator(cfg, module_builder=build_modules)
    await orch.initialize()

    gm = orch.graph_memory
    for i in range(6):  # >= dmn_top_k non-hub, non-thought seed nodes for imagination
        await gm.add_node(
            f"app:svc{i}", NodeType.APP, {"label": f"service {i}", "relevance_score": float(9 - i)}
        )

    with log_path.open("w", encoding="utf-8") as logfile:
        np_logging.configure("WARNING", stream=logfile)
        await orch.start()
        dmn = next(m for m in orch._modules if m.name == "idle")

        t0 = time.perf_counter()
        orch.event_bus.publish(Event(event_type=EventType.IDLE_DETECTED, source="validate"))
        for _ in range(200):  # wait for the DMN to pick up the event and spawn its task
            await asyncio.sleep(0.01)
            if dmn._idle_task is not None:
                break
        await asyncio.gather(dmn._idle_task, return_exceptions=True)
        elapsed = time.perf_counter() - t0

        await asyncio.sleep(_BLOCK_S)  # let the last (abandoned) executor call drain
        daemon_ok = orch.is_running and orch.health_check().ok
        await orch.stop()

    idle_nodes = [nid for nid in gm.node_ids if nid.startswith("idle:")]
    print(f"elapsed         : {elapsed:.2f} s  (wall-clock budget {_WALL_CLOCK_S} s)")
    print(f"infer calls     : {backend.calls}  (budget {_INFER_BUDGET})")
    print(f"idle thoughts   : {len(idle_nodes)}  (expected {_EXPECTED_THOUGHTS})")
    print(f"_timeouts       : {dmn._timeouts}")
    print(f"_errors         : {dmn._errors}")
    print(f"daemon healthy  : {daemon_ok}")

    fails: list[str] = []
    if not (_ELAPSED_LOW <= elapsed <= _ELAPSED_HIGH):
        fails.append(f"cycle elapsed {elapsed:.2f}s outside [{_ELAPSED_LOW}, {_ELAPSED_HIGH}]s")
    if len(idle_nodes) != _EXPECTED_THOUGHTS:
        fails.append(f"produced {len(idle_nodes)} idle thoughts, expected {_EXPECTED_THOUGHTS}")
    if dmn._timeouts != 1:
        fails.append(f"_timeouts == {dmn._timeouts}, expected 1")
    if dmn._errors != 0:
        fails.append(f"_errors == {dmn._errors}, expected 0 (a budget overrun is not an error)")
    if dmn._cycles != 1:
        fails.append(f"_cycles == {dmn._cycles}, expected 1")
    if not daemon_ok:
        fails.append("daemon was not running / healthy after the timeout")

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(
        f"=== RESULT (PASS) — TimeoutError handled at {elapsed:.2f} s, "
        f"{len(idle_nodes)} thoughts (capped), daemon healthy ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
