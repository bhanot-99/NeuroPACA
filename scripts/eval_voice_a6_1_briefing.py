#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.1's real go/no-go: does the briefing measurably improve with voice
episodes on, versus off (`VISION_PHASES.md`'s A6.1 exit criterion, `VISION.md`
§9's H6)?

Runs `interface/briefing.compose_briefing` twice per hand-labelled day — once
normally, once with `include_voice=False` — against the real graph and
episode store, and scores each against a ground-truth set of which anchors
should have appeared that day. This is F3's harness pattern (`VISION_PHASES.md
§2`): a hand-labelled set plus an offline replay, not a live A/B on the
running daemon.

Ground truth (data/eval/briefing_ground_truth.json by default — gitignored,
you write it once real utterances exist):

    [
      {"date": "2026-09-20T09:00:00+00:00", "should_include": ["utterance:ab12cd34", "thread:xyz"]},
      ...
    ]

`date` is the `now` this day's briefing is composed as of; `should_include`
is every graph node id (`BriefingItem.anchor`) you'd have wanted in that
day's briefing. Precision/recall are computed on the *selected* (post
greedy-submodular) item set, not the raw candidate pool — the same items a
real briefing would have shown you.

Without a ground-truth file yet, this prints what to create and exits 0 —
there is nothing to score until one exists, and that needs real days of use
first (same reason `eval_voice_a6_1_review.py` needs real data).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from neuropaca.core.config import Config
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.interface.briefing import build_candidates, rank_candidates, select_greedy_submodular


def _precision_recall(selected_anchors: set[str], truth: set[str]) -> tuple[float, float]:
    if not selected_anchors:
        precision = 1.0 if not truth else 0.0
    else:
        precision = len(selected_anchors & truth) / len(selected_anchors)
    recall = len(selected_anchors & truth) / len(truth) if truth else 1.0
    return precision, recall


async def _score_one_day(
    gm: GraphMemory,
    store: EpisodeStore,
    config: Config,
    now: datetime,
    truth: set[str],
    *,
    include_voice: bool,
) -> set[str]:
    raw = await build_candidates(
        gm, store, now=now, last_briefing_seq=0, config=config, include_voice=include_voice
    )
    ranked = rank_candidates(gm, raw, focus_history=[], now=now, config=config)
    ranked = [item for item in ranked if item.value > 0.0]
    ranked.sort(key=lambda item: item.value, reverse=True)
    selected = select_greedy_submodular(
        ranked, gm, k=config.briefing_max_items, mu=config.briefing_similarity_mu
    )
    return {item.anchor for item in selected}


async def _run(
    graph_path: Path, episodes_path: Path, ground_truth: list[dict[str, object]]
) -> None:
    config = Config(inference_backend="fake")
    gm = GraphMemory.get_instance(persistence_path=str(graph_path))
    await gm.load()
    store = EpisodeStore(episodes_path)
    await store.start()

    totals = {"with": [0.0, 0.0], "without": [0.0, 0.0]}  # [precision_sum, recall_sum]
    n = 0
    try:
        for day in ground_truth:
            now = datetime.fromisoformat(str(day["date"]))
            truth = {str(a) for a in day["should_include"]}  # type: ignore[union-attr]

            with_voice = await _score_one_day(gm, store, config, now, truth, include_voice=True)
            without_voice = await _score_one_day(gm, store, config, now, truth, include_voice=False)

            p_w, r_w = _precision_recall(with_voice, truth)
            p_wo, r_wo = _precision_recall(without_voice, truth)
            print(
                f"{day['date']}  with-voice P={p_w:.2f} R={r_w:.2f}  "
                f"without-voice P={p_wo:.2f} R={r_wo:.2f}"
            )

            totals["with"][0] += p_w
            totals["with"][1] += r_w
            totals["without"][0] += p_wo
            totals["without"][1] += r_wo
            n += 1
    finally:
        await store.stop()

    if n == 0:
        print("Ground-truth file has no entries.")
        return
    print(f"\n{'=' * 60}")
    print(f"Averaged over {n} day(s):")
    print(f"  with voice    P={totals['with'][0] / n:.3f}  R={totals['with'][1] / n:.3f}")
    print(f"  without voice P={totals['without'][0] / n:.3f}  R={totals['without'][1] / n:.3f}")
    dp = totals["with"][0] / n - totals["without"][0] / n
    dr = totals["with"][1] / n - totals["without"][1] / n
    print(f"  delta         P={dp:+.3f}  R={dr:+.3f}")
    print("\nThis delta is A6.1's actual exit criterion — positive or negative, it's the answer.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--graph-path", default=Path("data/graph.json"), type=Path)
    parser.add_argument("--episodes-path", default=Path("data/episodes.sqlite"), type=Path)
    parser.add_argument(
        "--ground-truth", default=Path("data/eval/briefing_ground_truth.json"), type=Path
    )
    args = parser.parse_args()

    if not args.ground_truth.is_file():
        print(f"No ground-truth file at {args.ground_truth}.")
        print("Create it once you've used voice for a few real days — see this script's own")
        print("docstring for the exact shape (a list of {date, should_include} entries).")
        return 0

    ground_truth = json.loads(args.ground_truth.read_text())
    asyncio.run(_run(args.graph_path, args.episodes_path, ground_truth))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# gen-ref: 677a62ec
