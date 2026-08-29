"""Generate the synthetic (signal + K nodes) fixtures for the coherence ablation.

    python spikes/b0_bitnet/gen_fixtures.py            # writes fixtures/ablation.jsonl
    python spikes/b0_bitnet/gen_fixtures.py --per-k 25 --seed 7

Synthetic / fictional data only — no real paths, no real machine numbers
(rules.md §4.1 item 8). Deterministic for a given seed so a run is reproducible.

Fixture line schema (JSONL):
    fixture_id          : str
    signal              : str   one of SIGNALS
    confidence          : float 0..1
    nodes               : [ {alias,label,node_type,score,attrs{str:str}}, ... ]
    expected_citations  : [str] aliases a correct grounded answer cites
                                (empty => the model should abstain)
    weak                : bool  deliberately unsupportive context
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from _common import SPIKE_DIR

K_VALUES = (1, 3, 5, 8)
SIGNALS = ("HIGH_LOAD", "FOCUS_SESSION", "DISTRACTION", "IDLE")

# (label, node_type, {attr: value}) — a small fictional pool.
RELEVANT_POOL = [
    ("bundler-watch", "APP", {"cpu_avg": "93%"}),
    ("~/proj/alpha/src", "FILE", {"edits_today": "31"}),
    ("deep-work", "SESSION", {"minutes": "47"}),
    ("linter-daemon", "APP", {"cpu_avg": "88%"}),
    ("test-runner", "APP", {"cpu_avg": "77%"}),
    ("~/proj/beta/notes.md", "FILE", {"edits_today": "12"}),
    ("editor-app", "APP", {"focus_min": "52"}),
    ("chat-client", "APP", {"switches": "9"}),
    ("music-player", "APP", {"switches": "6"}),
    ("compile-job", "TASK", {"runs_today": "18"}),
]
DISTRACTOR_POOL = [
    ("archived-report.pdf", "FILE", {"last_open": "41d ago"}),
    ("old-migration", "TASK", {"runs_today": "0"}),
    ("stale-cache-dir", "FILE", {"age": "60d"}),
    ("weather-widget", "APP", {"switches": "1"}),
    ("backup-cron", "TASK", {"runs_today": "1"}),
]


def _node(alias: str, spec: tuple[str, str, dict[str, str]], score: float) -> dict[str, object]:
    label, node_type, attrs = spec
    return {
        "alias": alias,
        "label": label,
        "node_type": node_type,
        "score": round(score, 1),
        "attrs": attrs,
    }


def _make_case(rng: random.Random, k: int, idx: int) -> dict[str, object]:
    weak = idx % 5 == 0  # ~20% of each K bucket is a deliberate abstain test
    signal = rng.choice(SIGNALS)
    aliases = [f"n{i + 1}" for i in range(k)]

    if weak:
        specs = rng.sample(DISTRACTOR_POOL, k=min(k, len(DISTRACTOR_POOL)))
        while len(specs) < k:
            specs.append(rng.choice(DISTRACTOR_POOL))
        nodes = [_node(a, s, rng.uniform(0.5, 3.0)) for a, s in zip(aliases, specs, strict=True)]
        expected: list[str] = []
    else:
        n_relevant = 1 if k == 1 else rng.randint(1, min(2, k))
        rel = rng.sample(RELEVANT_POOL, k=n_relevant)
        distract = rng.sample(DISTRACTOR_POOL, k=min(k - n_relevant, len(DISTRACTOR_POOL)))
        specs = rel + distract
        while len(specs) < k:
            specs.append(rng.choice(RELEVANT_POOL))
        rng.shuffle(specs)
        nodes = []
        expected = []
        for alias, spec in zip(aliases, specs, strict=True):
            is_rel = spec in rel
            score = rng.uniform(6.5, 9.0) if is_rel else rng.uniform(1.0, 4.0)
            nodes.append(_node(alias, spec, score))
            if is_rel:
                expected.append(alias)

    return {
        "fixture_id": f"k{k}-{idx:02d}{'-weak' if weak else ''}",
        "signal": signal,
        "confidence": round(rng.uniform(0.6, 0.9), 2),
        "nodes": nodes,
        "expected_citations": expected,
        "weak": weak,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-k", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=SPIKE_DIR / "fixtures" / "ablation.jsonl")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        fh.write(f"# generated: per_k={args.per_k} seed={args.seed}\n")
        count = 0
        for k in K_VALUES:
            for idx in range(1, args.per_k + 1):
                fh.write(json.dumps(_make_case(rng, k, idx)) + "\n")
                count += 1
    print(f"wrote {count} fixtures ({len(K_VALUES)} K-buckets) -> {args.out}")


if __name__ == "__main__":
    main()
