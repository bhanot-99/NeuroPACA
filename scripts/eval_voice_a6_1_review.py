#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.1 review tool — hand-label voice-derived episodes for correctness.

`VISION_PHASES.md`'s A6.1 exit criterion: "a week of typed use produces
episodes a hand-labelled check agrees are correct." This script automates
the *review*, not the judgment: it pulls every voice utterance's classified
episode from the episode store, shows it to you, and asks whether the
classification is right. The answer is always yours; the script only
removes the manual-digging-through-the-store part.

Usage:
    scripts/eval_voice_a6_1_review.py [--episodes-path PATH] [--since DAYS]
        [--labels-file PATH] [--save-labels PATH]

Without --labels-file, prompts interactively (y/n/q per utterance) and
prints an agreement report at the end, overall and per classified category.
With --labels-file (one y/n per line, in episode order — the same shape
--save-labels writes), replays those answers instead of prompting, so a
review session can be scripted or rerun without retyping every answer.

This project is public (rules.md §6-adjacent norm): the default output
location, --save-labels, is under data/eval/, which is gitignored — nothing
this script prints or saves is meant to leave the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
from datetime import UTC, datetime, timedelta
from pathlib import Path

from neuropaca.core.episodes import EpisodeRecord, EpisodeStore


async def _load_utterance_facts(episodes_path: Path, since: datetime) -> list[EpisodeRecord]:
    store = EpisodeStore(episodes_path)
    await store.start()
    try:
        rows = await store.between(since, datetime.now(UTC))
        return [r for r in rows if r.kind == "plugin_fact" and r.subject.startswith("utterance:")]
    finally:
        await store.stop()


def _prompt(text: str, category: str, cited: str | None) -> bool:
    print(f"\n  utterance : {text}")
    print(f"  category  : {category}")
    print(f"  cited node: {cited or '(none)'}")
    while True:
        ans = input("  correct? [y/n/q] ").strip().lower()
        if ans in ("y", "n"):
            return ans == "y"
        if ans == "q":
            raise KeyboardInterrupt
        print("  please answer y, n, or q")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--episodes-path", default=Path("data/episodes.sqlite"), type=Path)
    parser.add_argument("--since", type=int, default=7, help="days to look back (default 7)")
    parser.add_argument("--labels-file", type=Path, default=None)
    parser.add_argument("--save-labels", type=Path, default=Path("data/eval/voice_a6_1_labels.txt"))
    args = parser.parse_args()

    since = datetime.now(UTC) - timedelta(days=args.since)
    rows = asyncio.run(_load_utterance_facts(args.episodes_path, since))
    if not rows:
        print(f"No voice-utterance episodes found since {since.isoformat()}.")
        print(
            "Nothing to review yet — turn on voice_enabled, type a few utterances, and come back."
        )
        return 0

    replay: list[bool] | None = None
    if args.labels_file:
        lines = [
            ln.strip().lower() for ln in args.labels_file.read_text().splitlines() if ln.strip()
        ]
        replay = [ln == "y" for ln in lines]
        if len(replay) != len(rows):
            print(
                f"warning: {len(replay)} saved labels but {len(rows)} episodes"
                " — replaying as far as it goes"
            )

    answers: list[bool] = []
    by_category: dict[str, list[bool]] = collections.defaultdict(list)
    try:
        for i, row in enumerate(rows):
            category = str(row.attrs.get("voice_intent", "?"))
            cited = row.attrs.get("cited_node_id")
            text = row.object or ""
            if replay is not None and i < len(replay):
                correct = replay[i]
                mark = "y" if correct else "n"
                print(f"\n  utterance : {text}\n  category  : {category}\n  -> replayed: {mark}")
            else:
                correct = _prompt(text, category, cited)
            answers.append(correct)
            by_category[category].append(correct)
    except KeyboardInterrupt:
        print("\nStopped early.")

    n = len(answers)
    agree = sum(answers)
    print(f"\n{'=' * 50}")
    print(f"Reviewed: {n} / {len(rows)}")
    if n:
        print(f"Agreement: {agree}/{n} ({100 * agree / n:.0f}%)")
        for cat, vals in sorted(by_category.items()):
            print(f"  {cat:16s} {sum(vals)}/{len(vals)}")

    if answers and args.save_labels:
        args.save_labels.parent.mkdir(parents=True, exist_ok=True)
        args.save_labels.write_text("\n".join("y" if a else "n" for a in answers) + "\n")
        print(f"\nLabels saved to {args.save_labels}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# gen-ref: 60c4d97d
