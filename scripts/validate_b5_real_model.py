#!/usr/bin/env python3
"""B5 · Exit Criterion 1 — real-model coherence + concurrent RAM (phases.md B5, D-12).

Loads **both** models into one `BitNetRuntime`, sequentially, under the single
`_inference_lock`:

1. BitNet b1.58 2B4T  (the always-on loop model — L4/L6)
2. Qwen2.5-3B-Instruct Q4  (the interactive model — L9 `$` / `$?`)

then fires a synthetic `$?` query built from 3 graph nodes and asserts:

* the raw output parses the `$?` GBNF schema (`parse_answer` != None);
* **grounding** — the free text contains the label of the cited node
  (`parse_answer` enforces this; the script re-checks and prints it);
* **concurrent resident memory fits the 16 GB target box with wide margin.**
  Measured 2026-09-01: BitNet 2B4T ~1.37 GB + Qwen2.5-3B Q4_K_M ~3.25 GB =
  ~4.7 GB peak (~29 % of 16 GB). `--ram-budget-gb` defaults to 5.0; the real
  criterion is "leaves the user's session ample RAM", not a tight cap.

Run it on the target box (16 GB, Pop!_OS / COSMIC):

    NEUROPACA_QWEN=models/qwen2.5-3b-instruct-q4_k_m.gguf \\
      uv run --extra llama --extra spike python scripts/validate_b5_real_model.py

Exit 0 = all three assertions pass (records tok/s + RSS for memory.md).
Exit 1 = an assertion failed.
Exit 2 = a model file is missing — cannot validate (not a pass).
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import sys
import time
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
from neuropaca.core.enums import NodeType
from neuropaca.core.inference import create_backend, create_interactive_backend
from neuropaca.core.models import Node
from neuropaca.learning.prompts import (
    ANSWER_MAX_TOKENS,
    alias_nodes,
    build_answer_grammar,
    build_answer_prompt,
    parse_answer,
)

_RAM_BUDGET_GB = 5.0  # ~4.7 GB measured peak + headroom; the box has 16 GB (D-12, B5 close)
_DEFAULT_BITNET = "models/bitnet-2b4t-tq2_0.gguf"
_DEFAULT_QWEN = "models/qwen2.5-3b-instruct-q4_k_m.gguf"

# A synthetic 3-node episode — deliberately unambiguous so a coherent model
# cites n1 and names it. `n1`'s label is distinctive (not the bare "webpack" of
# the few-shot) so a *grounded* answer must have read it from context.
_NODES = [
    Node(id="app:esbuild", node_type=NodeType.APP, label="esbuild-service", relevance_score=8.4),
    Node(
        id="file:/home/u/src/app",
        node_type=NodeType.FILE,
        label="/home/u/src/app",
        relevance_score=7.1,
    ),
    Node(id="app:chrome", node_type=NodeType.APP, label="chrome", relevance_score=5.0),
]
_QUESTION = "what process is using the most CPU right now?"


def _rss_gb() -> float:
    return float(psutil.Process().memory_info().rss) / (1024**3)


async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("NEUROPACA_MODEL", _DEFAULT_BITNET))
    ap.add_argument("--interactive-model", default=os.environ.get("NEUROPACA_QWEN", _DEFAULT_QWEN))
    ap.add_argument("--ram-budget-gb", type=float, default=_RAM_BUDGET_GB)
    args = ap.parse_args()

    bitnet_path, qwen_path = Path(args.model), Path(args.interactive_model)
    if not bitnet_path.is_file():
        print(f"SKIP — BitNet model not found: {bitnet_path}", file=sys.stderr)
        return 2
    if not qwen_path.is_file():
        print(
            f"SKIP — Qwen interactive model not found: {qwen_path}\n"
            "  download a Qwen2.5-3B-Instruct Q4_K_M GGUF and pass "
            "--interactive-model / $NEUROPACA_QWEN",
            file=sys.stderr,
        )
        return 2

    cfg = Config(
        inference_backend="llama",
        model_path=str(bitnet_path),
        interactive_model_path=str(qwen_path),
    )
    runtime = BitNetRuntime(create_backend(cfg), create_interactive_backend(cfg))

    rss_base = _rss_gb()
    print(f"RSS baseline (no models):        {rss_base:6.3f} GB")

    t0 = time.perf_counter()
    assert await runtime.load_model_async(), "BitNet 2B4T failed to load"
    rss_bitnet = _rss_gb()
    print(
        f"RSS after BitNet 2B4T load:      {rss_bitnet:6.3f} GB   ({time.perf_counter() - t0:.1f}s)"
    )

    t0 = time.perf_counter()
    assert await runtime.load_interactive_model_async(), "Qwen2.5-3B Q4 failed to load"
    rss_concurrent = _rss_gb()
    print(
        f"RSS with BOTH models resident:   {rss_concurrent:6.3f} GB   "
        f"({time.perf_counter() - t0:.1f}s)   <-- concurrent peak"
    )

    # Fire the synthetic $? query through the interactive model.
    aliased = alias_nodes(_NODES)
    aliases = [a for a, _ in aliased]
    alias_to_id = {a: n.id for a, n in aliased}
    alias_to_label = {a: n.label for a, n in aliased}
    grammar = build_answer_grammar(aliases)
    prompt = build_answer_prompt(_QUESTION, aliased, live_snapshot="cpu 96% · mem 41%")

    t0 = time.perf_counter()
    raw = await runtime.infer_async(prompt, ANSWER_MAX_TOKENS, 0.3, grammar, interactive=True)
    dt = time.perf_counter() - t0
    rss_after_infer = _rss_gb()
    approx_tokens = max(1, len(raw) // 4)
    tok_s = approx_tokens / dt if dt > 0 else 0.0

    print("\n--- $? round-trip ---------------------------------------------------")
    print(f"raw output : {raw!r}")
    print(f"latency    : {dt:.2f}s   (~{tok_s:.1f} tok/s, {approx_tokens} tok est.)")
    print(f"RSS after  : {rss_after_infer:6.3f} GB")

    # `parse_answer` IS the exit gate: it enforces schema parse AND grounding
    # (the sentence must reference a cited node's label — rules.md §4.1). A
    # non-None result means both criteria passed with the shipped logic.
    answer = parse_answer(raw, alias_to_id, alias_to_label)

    # ---- assertions -------------------------------------------------------
    failures: list[str] = []
    if answer is None:
        failures.append("GBNF parse / grounding gate rejected the output (parse_answer -> None)")
    else:
        cited_labels = [n.label for n in _NODES if n.id in answer.cited_node_ids]
        exact = any(lbl.lower() in answer.text.lower() for lbl in cited_labels)
        print(
            f"parsed     : text={answer.text!r} cited={answer.cited_node_ids} "
            f"conf={answer.confidence}"
        )
        print(f"grounding  : gate PASSED · exact-label-substring={exact} (cited {cited_labels})")

    peak = max(rss_concurrent, rss_after_infer)
    if peak >= args.ram_budget_gb:
        failures.append(f"concurrent RSS {peak:.3f} GB >= budget {args.ram_budget_gb} GB")

    print("\n=== RESULT ========================================================")
    print(f"  concurrent peak RSS : {peak:.3f} GB  (budget {args.ram_budget_gb} GB)")
    print(f"  Qwen throughput     : ~{tok_s:.1f} tok/s")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  PASS — coherence + grounding + RAM budget all met")
    print(
        f"\n  record in memory.md: Qwen2.5-3B Q4 ~{tok_s:.1f} tok/s, concurrent RSS {peak:.2f} GB"
    )
    del runtime
    gc.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
