"""B0 de-risking spike, part 1: does BitNet b1.58 2B4T fit the budget here?

Run ON THE TARGET MACHINE (phases.md B0):

    uv pip install -e ".[spike]"
    python spikes/b0_bitnet/benchmark_runtime.py \
        --model models/bitnet-b1.58-2b4t.Q4_0.gguf --minutes 30

Measures, and writes to results/runtime-<timestamp>.json:
  - RSS after model load (MB)   -> must leave room for the daemon + a dev session
                                   (PRD.md §9: ~1.1 GB target)
  - steady-state tokens/sec     -> feeds the L9 latency budget in design.md §6
  - RSS drift over the run      -> a leak here sinks the always-on premise
  - CPU package temperature     -> thermal throttling => the latency budget is a lie

Budgets are asserted at the end; a FAIL is a real B0 result, not a script bug.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
from _common import SPIKE_DIR, LlamaRunner, build_grammar

# Fixed, dependency-free prompt — we are measuring the runtime, not the answer.
_PROMPT = (
    "List three things a developer might check when a laptop fan gets loud. "
    "Answer in one short paragraph.\n"
)

RSS_LOAD_BUDGET_MB = 1400.0
RSS_DRIFT_BUDGET_MB = 150.0
MIN_TOKENS_PER_SEC = 5.0


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


def _package_temp_c() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return max(t.current for t in temps[key])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--n-threads", type=int, default=None)
    parser.add_argument("--sample-every", type=float, default=30.0)
    args = parser.parse_args()

    if not args.model.exists():
        raise SystemExit(f"model not found: {args.model}")

    rss_before = _rss_mb()
    runner = LlamaRunner(model_path=args.model, n_ctx=2048, n_threads=args.n_threads)
    runner.load()
    rss_after_load = _rss_mb()
    print(f"[rss] before={rss_before:.0f}MB  after_load={rss_after_load:.0f}MB")

    # A trivial one-alias grammar so the loop exercises constrained decoding too.
    grammar = build_grammar(["n1"])

    samples: list[dict[str, float | None]] = []
    tok_rates: list[float] = []
    start = time.monotonic()
    deadline = start + args.minutes * 60
    next_sample = start

    while time.monotonic() < deadline:
        completion = runner.generate(_PROMPT, grammar, max_tokens=96)
        tok_rates.append(completion.tokens_per_second)

        if time.monotonic() >= next_sample:
            sample: dict[str, float | None] = {
                "t_s": round(time.monotonic() - start, 1),
                "rss_mb": round(_rss_mb(), 1),
                "temp_c": _package_temp_c(),
                "tok_per_s": round(statistics.median(tok_rates[-10:]), 2),
            }
            samples.append(sample)
            print(f"[sample] {sample}")
            next_sample += args.sample_every

    rss_end = _rss_mb()
    median_rate = statistics.median(tok_rates) if tok_rates else 0.0
    rss_drift = rss_end - rss_after_load
    temps = [s["temp_c"] for s in samples if s["temp_c"] is not None]

    verdict = {
        "rss_after_load_ok": rss_after_load <= RSS_LOAD_BUDGET_MB,
        "rss_drift_ok": rss_drift <= RSS_DRIFT_BUDGET_MB,
        "tok_per_s_ok": median_rate >= MIN_TOKENS_PER_SEC,
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model.name,
        "minutes": args.minutes,
        "n_threads": args.n_threads,
        "rss_before_mb": round(rss_before, 1),
        "rss_after_load_mb": round(rss_after_load, 1),
        "rss_end_mb": round(rss_end, 1),
        "rss_drift_mb": round(rss_drift, 1),
        "median_tok_per_s": round(median_rate, 2),
        "temp_c_min": min(temps) if temps else None,
        "temp_c_max": max(temps) if temps else None,
        "samples": samples,
        "budgets": {
            "rss_after_load_mb": RSS_LOAD_BUDGET_MB,
            "rss_drift_mb": RSS_DRIFT_BUDGET_MB,
            "min_tok_per_s": MIN_TOKENS_PER_SEC,
        },
        "verdict": verdict,
        "pass": all(verdict.values()),
    }

    out_dir = SPIKE_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"runtime-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{'PASS' if report['pass'] else 'FAIL'}  ->  {out_path}")
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
