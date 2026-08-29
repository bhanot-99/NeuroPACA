"""B0 de-risking spike, part 2: the coherence ablation over context size K.

Run ON THE TARGET MACHINE, after `gen_fixtures.py` and after the runtime
benchmark (phases.md B0, problems.md 1.13):

    python spikes/b0_bitnet/coherence_ablation.py \
        --model models/bitnet-b1.58-2b4t.Q4_0.gguf

For each K in the fixture set it runs every (signal + K nodes) case through the
GBNF-constrained schema, greedy, with `cited_nodes` locked to that prompt's
aliases, and reports per K:

  - valid_parse_rate      should be ~1.0 (the grammar guarantees structure)
  - citation_accuracy(K)  mean over non-weak fixtures — the curve to plot
  - correct_abstain_rate  over weak fixtures — did it decline to answer?
  - grounded_rate         free text actually references a cited node's label

Decision (writes results/coherence-<timestamp>.json):
  - production K  = the knee of citation_accuracy(K)
  - if citation_accuracy never reaches ~0.80 at any K  ->  recommend the 3B Q4
    fallback for `$?` (rules.md §4.1, Architecture.md §11).
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from _common import (
    SPIKE_DIR,
    Fixture,
    GenerationResult,
    LlamaRunner,
    build_grammar,
    build_prompt,
    citation_accuracy,
    iter_fixtures_by_k,
    load_fixtures,
    score_output,
)

CITATION_ACCURACY_TARGET = 0.80
KNEE_MIN_DELTA = 0.03  # below this gain, a larger K is not worth the tokens


def _run_one(runner: LlamaRunner, fx: Fixture) -> GenerationResult:
    prompt = build_prompt(fx)
    grammar = build_grammar([n.alias for n in fx.nodes])  # pure string work, pre-lock
    raw, latency = runner.generate(prompt, grammar, max_tokens=110)
    return score_output(fx, raw, latency)


def _rate(flags: Iterable[object]) -> float:
    values = list(flags)
    return round(sum(bool(v) for v in values) / len(values), 3) if values else 0.0


def _summarise_k(fixtures: list[Fixture], results: list[GenerationResult]) -> dict[str, object]:
    pairs = list(zip(fixtures, results, strict=True))
    weak = [(f, r) for f, r in pairs if f.weak]
    strong = [(f, r) for f, r in pairs if not f.weak]

    acc_values = [a for f, r in strong if (a := citation_accuracy(f, r)) is not None]
    return {
        "n": len(results),
        "valid_parse_rate": _rate(r.parse_ok for r in results),
        "citations_valid_rate": _rate(r.citations_valid for r in results),
        "citation_accuracy": round(statistics.mean(acc_values), 3) if acc_values else None,
        "grounded_rate": _rate(r.grounded for _, r in strong) if strong else None,
        "correct_abstain_rate": (
            _rate(r.abstained and r.grounded for _, r in weak) if weak else None
        ),
        "mean_latency_s": round(statistics.mean(r.latency_s for r in results), 2),
    }


def _pick_knee(acc_by_k: dict[int, float | None]) -> int | None:
    usable = {k: v for k, v in acc_by_k.items() if v is not None}
    if not usable:
        return None
    ordered = sorted(usable)
    best_k = ordered[0]
    for prev, cur in itertools.pairwise(ordered):
        if usable[cur] - usable[prev] >= KNEE_MIN_DELTA:
            best_k = cur
        else:
            break
    return best_k


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=SPIKE_DIR / "fixtures" / "ablation.jsonl")
    parser.add_argument("--n-threads", type=int, default=None)
    args = parser.parse_args()

    if not args.model.exists():
        raise SystemExit(f"model not found: {args.model}")
    if not args.fixtures.exists():
        raise SystemExit(f"no fixtures — run gen_fixtures.py first ({args.fixtures})")

    fixtures = load_fixtures(args.fixtures)
    runner = LlamaRunner(model_path=args.model, n_ctx=2048, n_threads=args.n_threads)
    runner.load()

    per_k: dict[int, dict[str, object]] = {}
    for k, k_fixtures in iter_fixtures_by_k(fixtures):
        results = [_run_one(runner, fx) for fx in k_fixtures]
        per_k[k] = _summarise_k(k_fixtures, results)
        print(f"K={k}: {per_k[k]}")

    acc_by_k: dict[int, float | None] = {
        k: per_k[k]["citation_accuracy"]  # type: ignore[misc]
        for k in per_k
    }
    knee = _pick_knee(acc_by_k)
    best_acc = max((v for v in acc_by_k.values() if v is not None), default=0.0)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model.name,
        "fixtures": str(args.fixtures),
        "per_k": per_k,
        "citation_accuracy_target": CITATION_ACCURACY_TARGET,
        "recommended_production_k": knee,
        "meets_target": bool(best_acc >= CITATION_ACCURACY_TARGET),
        "recommendation": (
            f"set _build_context() K = {knee}"
            if best_acc >= CITATION_ACCURACY_TARGET
            else "constrained 2B4T under target at every K -> use 3B Q4 for `$?` (rules.md §4.1)"
        ),
    }

    out_dir = SPIKE_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"coherence-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n->  {out_path}")
    print(report["recommendation"])


if __name__ == "__main__":
    main()
