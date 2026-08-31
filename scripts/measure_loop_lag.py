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

import time

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_PROBE_INTERVAL_S = 0.01
_LAG_LIMIT_MS = 50.0
_BLAST_BATCH = 100
_BLAST_DISTINCT_FILES = 200
_INFER_STRESS_SECONDS = 10.0
_STRESS_PROMPT = "Summarise the current system state in one sentence.\n"


class _BlockingFakeBackend:
    """Stands in for `LlamaCppBackend` when llama-cpp-python is not installed.
    `infer()` genuinely blocks its thread for ~2 s — enough that, if the offload
    were broken, the probe would immediately see > 50 ms lag."""

    def __init__(self) -> None:
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        time.sleep(0.5)
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        time.sleep(2.0)  # blocking CPU work, off the loop
        return '{"cited_node_id": null, "insight_category": "routine"}'

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return 0.0


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


def _make_infer_backend() -> object:
    """Real `LlamaCppBackend` if the wheel + model are present, else a backend
    whose `infer()` blocks its thread for ~2 s so the offload is still tested."""
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return _BlockingFakeBackend()
    from neuropaca.core.inference import create_backend

    cfg = Config(inference_backend="fake")  # placeholder; swapped below if a model exists
    model = _ROOT / "models"
    ggufs = sorted(model.glob("*.gguf")) if model.is_dir() else []
    if not ggufs:
        return _BlockingFakeBackend()
    cfg = Config(inference_backend="llama", model_path=str(ggufs[0]))
    return create_backend(cfg)


async def _infer_stress() -> int:
    """B4 exit clause I — a ~10 s inference must not stall the event loop.

    `BitNetRuntime.infer_async` offloads every call to its dedicated single-worker
    executor; this drives back-to-back calls for `_INFER_STRESS_SECONDS` while a
    10 ms probe measures loop lag."""
    runtime = BitNetRuntime(_make_infer_backend())  # type: ignore[arg-type]
    loaded = await runtime.load_model_async()
    print(f"infer-stress backend: {type(runtime._backend).__name__}  loaded={loaded}")

    stop = asyncio.Event()
    samples: list[float] = []
    probe_task = asyncio.create_task(_probe(samples, stop))
    await asyncio.sleep(0.2)  # let the probe settle

    calls = 0
    deadline = asyncio.get_running_loop().time() + _INFER_STRESS_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await runtime.infer_async(_STRESS_PROMPT, 256, 0.0, None)
        calls += 1

    stop.set()
    probe_task.cancel()
    try:
        await probe_task
    except asyncio.CancelledError:
        pass
    runtime.unload_model()

    if len(samples) < 10:
        print(f"only {len(samples)} probe samples — inference finished too fast")
        return 1
    samples.sort()
    mean = statistics.fmean(samples)
    p99 = samples[min(len(samples) - 1, int(len(samples) * 0.99))]
    peak = samples[-1]
    print(
        f"loop lag over {len(samples)} probes during {calls} inference call(s) "
        f"(~{_INFER_STRESS_SECONDS:.0f}s):"
    )
    print(
        f"  mean {mean:6.2f} ms   p99 {p99:6.2f} ms   max {peak:6.2f} ms   "
        f"(limit {_LAG_LIMIT_MS:.0f} ms)"
    )
    if peak < _LAG_LIMIT_MS:
        print("PASS")
        return 0
    print("FAIL — inference stalled the event loop; the executor offload is broken")
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
    parser.add_argument(
        "--infer-stress",
        action="store_true",
        help="run a ~10 s backend inference and assert loop lag stays < 50 ms (B4 exit I)",
    )
    args = parser.parse_args()
    if args.infer_stress:
        sys.exit(asyncio.run(_infer_stress()))
    sys.exit(asyncio.run(_run(args.seconds, args.big_graph)))


if __name__ == "__main__":
    main()
